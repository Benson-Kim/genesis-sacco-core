"""core/exports engine tests: truncation, row caps, off-loop batches.

No database: the engine contract is exercised against an in-memory
keyset query that records exactly what the engine asked for. All
oracles hand-computed in comments (never captured from the engine).
"""

import asyncio
import statistics
import time
from typing import Any

from httpx import ASGITransport, AsyncClient

from genesis.api.app import create_app
from genesis.application.exports import run_export
from genesis.application.reports import ReportCursor, ReportQuery
from genesis.domain.documents import Cell


class RecordingQuery:
    """Keyset query over range(total); records every requested limit."""

    def __init__(self, total: int) -> None:
        self.rows = list(range(total))
        self.requested_limits: list[int] = []

    def query(self) -> ReportQuery:
        async def fetch(cursor: ReportCursor | None, limit: int) -> list[Any]:
            self.requested_limits.append(limit)
            start = int(cursor) + 1 if cursor is not None else 0  # type: ignore[arg-type]
            return self.rows[start : start + limit]

        def cursor_key(raw: Any) -> ReportCursor:
            return int(raw)

        def to_cells(raw: Any) -> tuple[Cell, ...]:
            return (raw,)

        return ReportQuery(fetch=fetch, cursor_key=cursor_key, to_cells=to_cells)


def test_no_truncation_below_the_cap() -> None:
    async def run() -> None:
        source = RecordingQuery(total=7)
        seen: list[list[tuple[Cell, ...]]] = []
        result = await run_export(source.query(), 3, row_cap=100, on_batch=seen.append)
        # Hand-computed: 7 rows, cap 100 -> everything, no truncation.
        # Delivery is observed through on_batch - since P13.17(d) the
        # run carries no rows (DSA-4); the oracle is unchanged.
        assert result.row_count == 7
        assert result.truncated is False
        assert [cells for batch in seen for cells in batch] == [(i,) for i in range(7)]
        assert result.row_limit == 100

    asyncio.run(run())


def test_exactly_cap_rows_is_not_truncated() -> None:
    async def run() -> None:
        source = RecordingQuery(total=5)
        result = await run_export(source.query(), 2, row_cap=5)
        # Hand-computed: 5 rows and cap 5. The engine must OBSERVE that
        # no 6th row exists (the +1 fetch) rather than infer truncation
        # from "cap reached". An engine that fetched exactly `want`
        # rows and flagged truncation on a full final batch would
        # report truncated=True here (falsifiability of the +1 rule).
        assert result.row_count == 5
        assert result.truncated is False

    asyncio.run(run())


def test_truncates_exactly_at_the_cap() -> None:
    async def run() -> None:
        source = RecordingQuery(total=6)
        seen: list[list[tuple[Cell, ...]]] = []
        result = await run_export(source.query(), 2, row_cap=5, on_batch=seen.append)
        # Hand-computed: 6 rows, cap 5 -> exactly 5 kept, flagged.
        # (Same DSA-4 note as above: delivery observed via on_batch.)
        assert result.row_count == 5
        assert result.truncated is True
        assert [cells for batch in seen for cells in batch] == [(i,) for i in range(5)]

    asyncio.run(run())


def test_every_fetch_asks_for_one_extra_row() -> None:
    async def run() -> None:
        source = RecordingQuery(total=7)
        await run_export(source.query(), 3, row_cap=5)
        # Hand-computed: batch 1 wants 3 -> asks 4; batch 2 wants
        # min(3, 5-3)=2 -> asks 3. Cap reached with a 6th row seen.
        assert source.requested_limits == [4, 3]

    asyncio.run(run())


def test_projection_and_batch_delivery_order() -> None:
    async def run() -> None:
        source = RecordingQuery(total=5)
        seen: list[list[tuple[Cell, ...]]] = []
        result = await run_export(
            source.query(),
            2,
            row_cap=100,
            project=lambda cells: (cells[0], "x"),
            on_batch=seen.append,
        )
        # Hand-computed batches of 2,2,1 in keyset order.
        assert seen == [[(0, "x"), (1, "x")], [(2, "x"), (3, "x")], [(4, "x")]]
        assert result.row_count == 5

    asyncio.run(run())


def test_export_rendering_never_blocks_the_event_loop() -> None:
    """P13 EXIT criterion: export while serving latency-checked requests.

    on_batch simulates heavy rendering (0.2s CPU-ish sleep per batch,
    3 batches). Because run_export dispatches on_batch through
    asyncio.to_thread, the loop stays free: concurrent /healthz
    requests and a heartbeat must stay far below the 0.2s batch cost.
    Falsifiable: calling on_batch inline on the loop makes every
    heartbeat/request during a batch take >= 0.2s and this test fail.

    Loaded-runner tolerance (maintainer de-flake, batch-8 close-out;
    the batch-7 house-retry precedent): the 0.15s thresholds are
    UNCHANGED but are asserted on the MEDIAN OF THREE independent
    runs — see the rationale at the assertions.
    """

    async def run() -> tuple[float, float]:
        source = RecordingQuery(total=6)

        def slow_render(_batch: list[tuple[Cell, ...]]) -> None:
            time.sleep(0.2)

        max_gap = 0.0
        request_latencies: list[float] = []
        done = asyncio.Event()

        async def heartbeat() -> None:
            nonlocal max_gap
            while not done.is_set():
                started = time.monotonic()
                await asyncio.sleep(0.01)
                gap = time.monotonic() - started - 0.01
                max_gap = max(max_gap, gap)

        async def serve_requests() -> None:
            transport = ASGITransport(app=create_app())
            async with AsyncClient(transport=transport, base_url="http://test") as client:
                while not done.is_set():
                    started = time.monotonic()
                    response = await client.get("/healthz")
                    request_latencies.append(time.monotonic() - started)
                    assert response.status_code == 200
                    await asyncio.sleep(0.01)

        async def export() -> None:
            await run_export(source.query(), 2, row_cap=100, on_batch=slow_render)
            done.set()

        await asyncio.wait_for(asyncio.gather(export(), heartbeat(), serve_requests()), timeout=10)
        assert request_latencies, "no requests served during the export"
        p95 = sorted(request_latencies)[int(len(request_latencies) * 0.95)]
        return max_gap, p95

    # Event-loop stall oracle — threshold UNCHANGED at 0.15s, asserted
    # on the MEDIAN OF THREE independent runs (maintainer de-flake at
    # the batch-8 close-out; the batch-7 house-retry precedent —
    # assertions not weakened): the heartbeat coroutine is always in
    # flight, so ANY on-loop 0.2s batch inflates max_gap to ~0.19s
    # (0.2s stall minus the 0.01s sleep already accounted for) >= 0.15
    # on EVERY run — the deterministic falsifier for on-loop rendering,
    # so the median can never mask a real regression (all three runs
    # fail). What the median DOES absorb is single-run shared-runner
    # scheduling noise: isolated one-run outliers of 0.199s/0.156s/
    # 0.214s were observed on untouched export code (pipelines
    # 2738779546, 2739513995, 2739671889) while fresh runs of the same
    # tree were green.
    # Per-request latency stays p95, not max (flake disposition for
    # pipeline 2724154615: max() failed on a SINGLE 0.155s outlier
    # while every other latency was ~0.002-0.005s AND max_gap stayed
    # under threshold — runner noise on one request, not a loop
    # stall). Hand-computed falsifiability, unchanged: the export runs
    # 3 batches x 0.2s ~= 0.6s; on-loop rendering stalls the loop for
    # 0.6s of the ~0.62s run, so the requests in flight during each
    # batch read >= 0.2s and far more than 5% of requests exceed
    # 0.15 — every run's p95 fails AND every run's max_gap fails, so
    # both medians fail regardless.
    gaps: list[float] = []
    p95s: list[float] = []
    for _ in range(3):
        gap, p95 = asyncio.run(run())
        gaps.append(gap)
        p95s.append(p95)
    gap_median = statistics.median(gaps)
    assert gap_median < 0.15, (
        f"event loop stalled: median gap {gap_median:.3f}s of {gaps} during export"
    )
    p95_median = statistics.median(p95s)
    assert p95_median < 0.15, (
        f"p95 request latency: median {p95_median:.3f}s of {p95s} during export"
    )
