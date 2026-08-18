"""Cron one-shot overlap protection (per-worker Postgres advisory locks).

The cPanel cron schedule fires each `backend/scripts/cron_*.py` blindly;
`genesis.infrastructure.cron_lock` makes a still-running cycle and the
next tick mutually exclusive PER WORKER. Falsifiable both ways:

- a held lock makes a second same-key acquisition yield False (and the
  wired script exit 0 with a skip log, cycle untouched);
- a DIFFERENT worker's key is never blocked (one slow export must not
  starve dormancy);
- release is guaranteed on block exit — including when the guarded
  cycle raises — so a crashed run never wedges the schedule.

The key-registry checks are pure (no DB) and run on every pipeline.
"""

import asyncio
import importlib.util
import logging
import os
from pathlib import Path
from types import ModuleType

import pytest

from genesis.infrastructure.cron_lock import (
    CRON_LOCK_DORMANCY,
    CRON_LOCK_EXPORT,
    CRON_LOCK_IDEMPOTENCY_PURGE,
    CRON_LOCK_KEYS,
    CRON_LOCK_NAMESPACE,
    CRON_LOCK_OUTBOX,
    try_cron_lock,
)

_requires_db = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="requires a migrated database"
)

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"

#: (script module, its advisory-lock objid, the summary-log prefix) —
#: one row per one-shot entrypoint, so a new cron script without a lock
#: (or with a colliding key) is caught by the wiring tests below.
CRON_SCRIPTS: list[tuple[str, int, str]] = [
    ("cron_outbox", CRON_LOCK_OUTBOX, "outbox"),
    ("cron_idempotency_purge", CRON_LOCK_IDEMPOTENCY_PURGE, "idempotency"),
    ("cron_export", CRON_LOCK_EXPORT, "exports"),
    ("cron_dormancy", CRON_LOCK_DORMANCY, "dormancy"),
]


def _load_script(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, SCRIPTS_DIR / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# Pure key-registry guarantees (no DB): every worker has its OWN lock.
# ---------------------------------------------------------------------------


def test_every_worker_key_is_distinct() -> None:
    """Two workers sharing an objid would serialize unrelated cycles."""
    assert len(set(CRON_LOCK_KEYS.values())) == len(CRON_LOCK_KEYS)


def test_keys_fit_the_two_int_advisory_lock_form() -> None:
    """pg_try_advisory_lock(int, int) takes two int4 values."""
    int4_max = 2**31 - 1
    assert 0 < CRON_LOCK_NAMESPACE <= int4_max
    for objid in CRON_LOCK_KEYS.values():
        assert 0 < objid <= int4_max


def test_every_cron_script_is_registered() -> None:
    """A new one-shot entrypoint must join the lock registry."""
    scripts = {p.stem for p in SCRIPTS_DIR.glob("cron_*.py")}
    assert scripts == {name for name, _, _ in CRON_SCRIPTS}
    assert {key for _, key, _ in CRON_SCRIPTS} == set(CRON_LOCK_KEYS.values())


# ---------------------------------------------------------------------------
# Lock semantics against a real Postgres.
# ---------------------------------------------------------------------------


@_requires_db
def test_same_key_contention_yields_false_and_releases_on_exit() -> None:
    from db_helpers import factory

    async def run() -> None:
        f = factory()
        async with try_cron_lock(f, CRON_LOCK_EXPORT, worker="export") as first:
            assert first is True
            # A second invocation of the SAME worker (a fresh session —
            # a distinct Postgres backend) must be refused, not queued.
            async with try_cron_lock(f, CRON_LOCK_EXPORT, worker="export") as second:
                assert second is False
        # Released on exit: the next tick acquires normally.
        async with try_cron_lock(f, CRON_LOCK_EXPORT, worker="export") as again:
            assert again is True

    asyncio.run(run())


@_requires_db
def test_distinct_worker_keys_never_block_each_other() -> None:
    from db_helpers import factory

    async def run() -> None:
        f = factory()
        async with try_cron_lock(f, CRON_LOCK_EXPORT, worker="export") as export_lock:
            assert export_lock is True
            for key, worker in (
                (CRON_LOCK_OUTBOX, "outbox"),
                (CRON_LOCK_IDEMPOTENCY_PURGE, "idempotency_purge"),
                (CRON_LOCK_DORMANCY, "dormancy"),
            ):
                async with try_cron_lock(f, key, worker=worker) as other:
                    assert other is True

    asyncio.run(run())


@_requires_db
def test_lock_is_released_when_the_guarded_cycle_raises() -> None:
    """A crashed cycle must never wedge the next tick."""

    async def run() -> None:
        f = factory()
        with pytest.raises(RuntimeError, match="cycle crash"):
            async with try_cron_lock(f, CRON_LOCK_DORMANCY, worker="dormancy") as acquired:
                assert acquired is True
                raise RuntimeError("cycle crash (simulated)")
        async with try_cron_lock(f, CRON_LOCK_DORMANCY, worker="dormancy") as reacquired:
            assert reacquired is True

    asyncio.run(run())


# ---------------------------------------------------------------------------
# Script wiring: each one-shot skips (exit 0, no cycle) under a held lock.
# ---------------------------------------------------------------------------


@_requires_db
@pytest.mark.parametrize(("script", "key", "prefix"), CRON_SCRIPTS)
def test_one_shot_skips_and_logs_when_its_lock_is_held(
    script: str, key: int, prefix: str, caplog: pytest.LogCaptureFixture
) -> None:
    """Hold the worker's lock, run the real script main(): it must exit
    0 with the skip log and WITHOUT running its cycle (whose summary
    line would otherwise always be logged)."""
    module = _load_script(script)

    async def run() -> None:
        async with try_cron_lock(factory(), key, worker="test-holder") as held:
            assert held is True
            with caplog.at_level(logging.INFO):
                exit_code = await module.main()
            assert exit_code == 0

    asyncio.run(run())
    assert f"{prefix}: skipped — previous cycle still running" in caplog.text
    # The cycle itself never ran: no summary line (delivered=/purged=/
    # completed=/transitioned=) was emitted.
    assert "=" not in "".join(
        record.getMessage() for record in caplog.records if record.getMessage().startswith(prefix)
    )
