"""Observability suite (issue #4): scrubbing, request-id trust, metrics,
worker heartbeats, the ops endpoint, and the no-PII-in-logs gate."""

import asyncio
import json
import logging
import os
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from db_helpers import api_client, factory, latest_otp_code, seed_user, unique_email
from genesis.api.app import create_app
from genesis.application.auth import AuthContext, issue_access_token
from genesis.application.rbac import seed_permissions
from genesis.domain.rbac import Action, Module, seed_matrix
from genesis.errors import RateLimitedError
from genesis.infrastructure.tenancy import tenant_session
from genesis.infrastructure.worker_heartbeat import (
    record_worker_lock_skip,
    record_worker_success,
)
from genesis.logging import JsonFormatter, correlation_id_var, new_run_id, scrub
from genesis.observability import (
    AUTH_FAILURES_TOTAL,
    RATE_LIMITED_TOTAL,
    MetricsRegistry,
    metrics,
    router_label,
)
from genesis.settings import get_settings

requires_db = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="requires a migrated database"
)


# ---------------------------------------------------------------------------
# Scrubbing and the JSON formatter
# ---------------------------------------------------------------------------


def test_scrub_redacts_pii() -> None:
    text_in = "contact jane.doe@example.com or +254 712 345 678 now"
    out = scrub(text_in)
    assert "example.com" not in out
    assert "712" not in out
    assert "[redacted]" in out


def test_scrub_redacts_national_id_runs() -> None:
    """Kenyan national IDs are 7-8 digit runs — never loggable (issue #4)."""
    out = scrub("member presented id 12345678 at the counter")
    assert "12345678" not in out
    assert "[redacted]" in out
    # Short code-owned diagnostics (e.g. the 6-digit cron_lock
    # namespace) survive — the floor is deliberate.
    assert "815301" in scrub("advisory lock (815301, 1) held")


def test_json_formatter_includes_correlation_id_and_scrubs() -> None:
    token = correlation_id_var.set("cid-123")
    try:
        record = logging.LogRecord(
            "t", logging.INFO, __file__, 1, "hello test@example.com", None, None
        )
        payload = json.loads(JsonFormatter().format(record))
    finally:
        correlation_id_var.reset(token)
    assert payload["correlation_id"] == "cid-123"
    assert "test@example.com" not in payload["message"]


def test_json_formatter_emits_only_allowlisted_fields() -> None:
    """Structural no-PII guarantee: arbitrary record attributes (e.g. an
    ``extra={"email": ...}``) can never reach the serialized payload —
    the formatter emits a fixed allowlist of fields, nothing else."""
    record = logging.LogRecord("t", logging.INFO, __file__, 1, "hello", None, None)
    record.email = "leak@example.com"  # type: ignore[attr-defined]
    record.national_id = "12345678"  # type: ignore[attr-defined]
    payload = json.loads(JsonFormatter().format(record))
    assert set(payload) <= {"ts", "level", "logger", "message", "correlation_id", "exc"}
    assert "leak@example.com" not in json.dumps(payload)
    assert "12345678" not in json.dumps(payload)


def test_new_run_id_is_distinct_and_greppable() -> None:
    a, b = new_run_id(), new_run_id()
    assert a != b
    assert a.startswith("run-")


# ---------------------------------------------------------------------------
# Request-id trust (issue #4: honor inbound X-Request-ID only when trusted)
# ---------------------------------------------------------------------------


def test_request_id_honored_only_from_trusted_hop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TRUST_REQUEST_ID_HEADER", "1")
    get_settings.cache_clear()
    try:
        client = TestClient(create_app(), raise_server_exceptions=False)
        res = client.get("/healthz", headers={"x-request-id": "edge-1234abcd"})
        assert res.headers["x-request-id"] == "edge-1234abcd"
        # Even a trusted hop cannot smuggle a malformed id: too short
        # or bad charset falls back to a generated one.
        res = client.get("/healthz", headers={"x-request-id": "shrt"})
        assert res.headers["x-request-id"] != "shrt"
        res = client.get("/healthz", headers={"x-request-id": "has spaces and junk!"})
        assert res.headers["x-request-id"] != "has spaces and junk!"
    finally:
        monkeypatch.delenv("TRUST_REQUEST_ID_HEADER")
        get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Metrics registry + the error-handler counting seam
# ---------------------------------------------------------------------------


def test_router_label_is_bounded_and_template_derived() -> None:
    assert router_label("/members/{member_id}") == "members"
    assert router_label("/healthz") == "healthz"
    assert router_label("/") == "root"
    assert router_label(None) == "unrouted"


def test_metrics_registry_counters_and_histogram_p95() -> None:
    registry = MetricsRegistry()
    registry.inc_counter("x_total")
    registry.inc_counter("x_total", 2)
    assert registry.counter_value("x_total") == 3
    for _ in range(95):
        registry.observe_request("members", 0.04)
    for _ in range(5):
        registry.observe_request("members", 4.0)
    p95 = registry.request_p95("members")
    assert 0.025 < p95 <= 0.05
    out = registry.render_prometheus()
    assert 'genesis_http_request_duration_seconds_bucket{router="members",le="0.05"} 95' in out
    assert 'genesis_http_request_duration_seconds_bucket{router="members",le="+Inf"} 100' in out
    assert 'genesis_http_request_duration_seconds_count{router="members"} 100' in out
    assert 'genesis_http_request_p95_seconds{router="members"}' in out


def test_request_latency_lands_in_the_router_histogram(client: TestClient) -> None:
    assert client.get("/healthz").status_code == 200
    assert 'router="healthz"' in metrics.render_prometheus()


def test_429_and_401_counted_at_error_handler_seam() -> None:
    """Rate-limit trips and auth failures are counted where every 429/401
    funnels — the AppError handler — never inside rate_limit.py or
    api/auth.py internals (both owned by the open !3)."""
    app = create_app()

    @app.get("/_test/rate-limited", include_in_schema=False)
    async def _boom() -> None:  # pragma: no cover - body never returns
        raise RateLimitedError("test trip")

    client = TestClient(app, raise_server_exceptions=False)
    before_429 = metrics.counter_value(RATE_LIMITED_TOTAL)
    before_401 = metrics.counter_value(AUTH_FAILURES_TOTAL)
    assert client.get("/_test/rate-limited").status_code == 429
    assert client.get("/me/permissions").status_code == 401  # no bearer token
    assert metrics.counter_value(RATE_LIMITED_TOTAL) == before_429 + 1
    assert metrics.counter_value(AUTH_FAILURES_TOTAL) == before_401 + 1


# ---------------------------------------------------------------------------
# Worker heartbeats (DB): the countable form of the cron lock-skip signal
# ---------------------------------------------------------------------------


@requires_db
def test_worker_heartbeat_skip_counts_and_success_resets() -> None:
    async def run() -> None:
        worker = f"test-worker-{uuid.uuid4().hex[:8]}"
        assert await record_worker_lock_skip(factory(), worker) == 1
        assert await record_worker_lock_skip(factory(), worker) == 2
        await record_worker_success(factory(), worker)
        async with factory()() as session:
            row = (
                await session.execute(
                    text(
                        "SELECT consecutive_lock_skips, last_success_at "
                        "FROM worker_heartbeats WHERE worker = :worker"
                    ),
                    {"worker": worker},
                )
            ).one()
        assert row[0] == 0
        assert row[1] is not None

    asyncio.run(run())


# ---------------------------------------------------------------------------
# The ops endpoint (DB): auth-gated Prometheus surface
# ---------------------------------------------------------------------------


async def _seed_token(role_name: str) -> str:
    email = unique_email()
    tid, role_id = await seed_user(email, role_name=role_name)
    async with tenant_session(factory(), tid) as session:
        await seed_permissions(session, tid)
        user_id = (
            await session.execute(
                text("SELECT id FROM users WHERE email = :email"), {"email": email}
            )
        ).scalar_one()
    return issue_access_token(
        AuthContext(user_id=uuid.UUID(str(user_id)), tenant_id=tid, role_id=role_id)
    )


@requires_db
def test_ops_metrics_is_auth_gated_and_renders_prometheus() -> None:
    matrix = seed_matrix()
    assert matrix["System Admin"][Module.SETTINGS][Action.VIEW] is True
    assert matrix["Teller"][Module.SETTINGS][Action.VIEW] is False

    async def run() -> None:
        await record_worker_success(factory(), "outbox")
        admin_token = await _seed_token("System Admin")
        teller_token = await _seed_token("Teller")
        async with api_client() as client:
            res = await client.get("/ops/metrics")
            assert res.status_code == 401  # anonymous scrape dies
            res = await client.get(
                "/ops/metrics", headers={"authorization": f"Bearer {teller_token}"}
            )
            assert res.status_code == 403  # settings:view gate
            res = await client.get(
                "/ops/metrics", headers={"authorization": f"Bearer {admin_token}"}
            )
            assert res.status_code == 200
            assert res.headers["content-type"].startswith("text/plain")
            body = res.text
        assert "genesis_outbox_pending_events" in body
        assert "genesis_outbox_dead_events" in body
        assert "genesis_outbox_oldest_pending_age_seconds" in body
        assert 'genesis_worker_last_success_timestamp_seconds{worker="outbox"}' in body
        assert 'genesis_worker_consecutive_lock_skips{worker="outbox"}' in body
        assert "genesis_db_pool_size" in body
        assert "genesis_db_pool_checked_out" in body
        assert RATE_LIMITED_TOTAL in body
        assert AUTH_FAILURES_TOTAL in body

    asyncio.run(run())


def test_ops_metrics_is_hidden_from_the_openapi_contract() -> None:
    """The ops surface must never churn the generated web-client contract
    (web:spec-drift): it is internal, not part of the public schema."""
    schema = create_app().openapi()
    assert not any(path.startswith("/ops") for path in schema.get("paths", {}))


# ---------------------------------------------------------------------------
# The no-PII gate (issue #4): hot auth/member paths never log PII fields
# ---------------------------------------------------------------------------


class _CapturingHandler(logging.Handler):
    """Captures every record FORMATTED through the production JsonFormatter —
    the assertion surface is the exact bytes that would hit the log file."""

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.setFormatter(JsonFormatter())
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(self.format(record))


@requires_db
def test_no_pii_in_log_records_on_hot_auth_and_member_paths() -> None:
    """The enforced (not conventional) no-PII gate: seed distinctive PII
    values (email, phone, national id, names), drive the hot staff-auth,
    member-auth and member-read paths — including failure branches, which
    log the most — and assert NO formatted log record contains any of
    them. Any future log site interpolating one of these fields turns
    this red."""
    email = unique_email()
    phone = "+254712999888"
    national_id = "34567890"
    member_name = f"Wanjiku Kamau {uuid.uuid4().hex[:6]}"

    async def run() -> list[str]:
        tid, _ = await seed_user(email, phone=phone)
        member_id = uuid.uuid4()
        async with tenant_session(factory(), tid) as session:
            await session.execute(
                text(
                    "INSERT INTO members (id, tenant_id, member_no, type, name, phone, email) "
                    "VALUES (CAST(:id AS uuid), CAST(:tid AS uuid), :no, 'person', "
                    ":name, :phone, :email)"
                ),
                {
                    "id": str(member_id),
                    "tid": str(tid),
                    "no": f"GP-{member_id.hex[:6]}",
                    "name": member_name,
                    "phone": phone,
                    "email": email,
                },
            )

        handler = _CapturingHandler()
        client = api_client()  # create_app() runs configure_logging() first
        root = logging.getLogger()
        old_level = root.level
        root.addHandler(handler)
        root.setLevel(logging.DEBUG)
        try:
            headers = {"x-tenant-id": str(tid)}
            async with client:
                # Hot staff-auth path: request, a FAILED verify (the
                # branch that logs), then the real verify.
                res = await client.post("/auth/otp/request", json={"email": email}, headers=headers)
                assert res.status_code == 202
                code = await latest_otp_code(tid)
                wrong = "000000" if code != "000000" else "111111"
                res = await client.post(
                    "/auth/otp/verify",
                    json={"email": email, "code": wrong},
                    headers=headers,
                )
                assert res.status_code == 401
                res = await client.post(
                    "/auth/otp/verify",
                    json={"email": email, "code": code},
                    headers=headers,
                )
                assert res.status_code == 200
                token = res.json()["access_token"]
                # Hot member-auth path: phone and national-id
                # identifiers travel it; the response never reveals
                # whether a credential exists.
                for identifier in (phone, national_id):
                    res = await client.post(
                        "/member/auth/otp/request",
                        json={"identifier": identifier},
                        headers=headers,
                    )
                    assert res.status_code in (202, 401, 422, 429)
                # Hot member-read path (staff): names/phones in the
                # RESPONSE are fine — in the LOGS they are not.
                res = await client.get("/members", headers={"authorization": f"Bearer {token}"})
                assert res.status_code == 200
        finally:
            root.removeHandler(handler)
            root.setLevel(old_level)
        return handler.lines

    lines = asyncio.run(run())
    assert lines, "expected the hot paths to emit at least one log record"
    joined = "\n".join(lines)
    for pii in (email, phone, phone.removeprefix("+"), national_id, member_name):
        assert pii not in joined, f"PII value leaked into log records: {pii!r}"
