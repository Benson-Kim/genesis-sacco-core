"""P13.17(d) / DSA-4 — incremental PDF rendering (FM4 suite).

Named failure mode FM4: memory regression — the export engine keeping
a third in-memory copy of the row set (raw cells alongside the CSV and
PDF buffers). The guard is structural: ExportRun no longer HAS a rows
field (the engine hands each batch to on_batch and releases it), and
the PDF is folded batch-by-batch by PdfBuilder.

No database needed: the page model is pure domain code.

Falsifiability, per test (anti-reward-hacking rules):

  * test_export_run_carries_no_rows fails the moment a rows
    accumulation is reintroduced on the run result — the exact DSA-4
    regression;
  * the byte-equality tests fail for any batch-dependent rendering: a
    builder that paginated per batch (instead of folding lines and
    paginating once at finish) yields different bytes for different
    splits of the SAME rows, and can never equal the one-shot render
    for every split simultaneously. Page boundaries are exercised on
    both sides: the narrow fixture spans 3 pages with batch splits
    that straddle page breaks; the wide fixture emits multi-line
    records whose lines cross page boundaries mid-record.

The existing export-latency test (test_run_export.py::
test_export_rendering_never_blocks_the_event_loop) is deliberately
UNTOUCHED by P13.17(d) — its oracles (max_gap, p95) hold unchanged on
the batch-released engine, which is itself part of the FM4 exit
criterion.
"""

import dataclasses
from datetime import UTC, date, datetime
from decimal import Decimal

from genesis.application.exports import ExportRun
from genesis.domain.documents import Cell, PdfBuilder, render_pdf

#: Narrow report: 4 columns <= _WIDE_REPORT_COLUMNS (8), one line per
#: row. 120 rows + 1 header line = 121 lines, > 3 pages of 36 lines.
_NARROW_HEADERS = ("Member", "Account", "Debits", "Credits")

#: Wide report: 10 columns > 8 -> record style (10 "key: value" lines
#: + 1 blank separator per row), so 12 rows = 132 lines across pages
#: with page breaks landing MID-record.
_WIDE_HEADERS = tuple(f"Column {i}" for i in range(10))


def _narrow_rows(count: int) -> list[tuple[Cell, ...]]:
    """Deterministic mixed-type rows (Decimal/str/date/None cells)."""
    return [
        (
            f"GP-{i:04d}",
            "cash.bank" if i % 2 else None,
            Decimal(i) / 100,
            date(2026, 1 + (i % 12), 1),
        )
        for i in range(count)
    ]


def _wide_rows(count: int) -> list[tuple[Cell, ...]]:
    return [
        tuple(
            f"value {i}-{j}" if j % 3 else datetime(2026, 1, 1, tzinfo=UTC)
            for j in range(len(_WIDE_HEADERS))
        )
        for i in range(count)
    ]


def _split(rows: list[tuple[Cell, ...]], sizes: list[int]) -> list[list[tuple[Cell, ...]]]:
    batches: list[list[tuple[Cell, ...]]] = []
    at = 0
    for size in sizes:
        batches.append(rows[at : at + size])
        at += size
    assert at == len(rows), "fixture split must cover every row exactly once"
    return batches


def test_export_run_carries_no_rows() -> None:
    """The DSA-4 structural guard: outcome metadata only. Reintroducing
    a rows accumulation on the run result fails this immediately."""
    assert {field.name for field in dataclasses.fields(ExportRun)} == {
        "row_count",
        "truncated",
        "row_limit",
    }


def test_narrow_report_batched_pdf_equals_one_shot_bytes() -> None:
    rows = _narrow_rows(120)
    one_shot = render_pdf(title="TB", meta="rows 120", headers=_NARROW_HEADERS, rows=rows)
    # 121 lines / 36 per page -> 4 pages: multi-page is exercised.
    assert one_shot.count(b"/Type /Page ") == 4
    for sizes in ([1] * 120, [3] * 40, [7] * 17 + [1], [36, 36, 36, 12], [120]):
        builder = PdfBuilder(_NARROW_HEADERS)
        for batch in _split(rows, sizes):
            builder.add_rows(batch)
        assert builder.finish(title="TB", meta="rows 120") == one_shot, sizes


def test_wide_report_batched_pdf_equals_one_shot_bytes() -> None:
    """Record-style rendering: each row spans 11 lines, so page breaks
    land mid-record — a per-batch paginator cannot reproduce this."""
    rows = _wide_rows(12)
    one_shot = render_pdf(title="Register", meta="rows 12", headers=_WIDE_HEADERS, rows=rows)
    assert one_shot.count(b"/Type /Page ") == 4  # 132 lines / 36 -> 4 pages
    for sizes in ([1] * 12, [5, 4, 3], [12]):
        builder = PdfBuilder(_WIDE_HEADERS)
        for batch in _split(rows, sizes):
            builder.add_rows(batch)
        assert builder.finish(title="Register", meta="rows 12") == one_shot, sizes


def test_empty_report_placeholder_matches_one_shot() -> None:
    """Zero rows renders the '(no rows)' placeholder identically
    whether nothing was ever added or an empty batch was folded."""
    one_shot = render_pdf(title="T", meta="rows 0", headers=_NARROW_HEADERS, rows=[])
    untouched = PdfBuilder(_NARROW_HEADERS)
    empty_batch = PdfBuilder(_NARROW_HEADERS)
    empty_batch.add_rows([])
    assert untouched.finish(title="T", meta="rows 0") == one_shot
    assert empty_batch.finish(title="T", meta="rows 0") == one_shot
    # _pdf_escape renders the placeholder as "\(no rows\)" inside the
    # content stream; probe the escaped-paren-free core.
    assert rb"\(no rows\)" in one_shot
