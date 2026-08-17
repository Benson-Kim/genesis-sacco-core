"""Pure document rendering for exports: CSV and PDF (P13, gates 1.3, 1.6).

Zero I/O — no imports from application, infrastructure, or api layers.

CSV formula-injection defence (P13 blocker b): any TEXT cell whose
value begins with ``=``, ``+``, ``-`` or ``@`` — including tab/CR/LF
prefixed variants used to smuggle those characters past naive checks —
is prefixed with a single quote so spreadsheet applications treat it
as inert text (the OWASP CSV-injection mitigation). Typed cells
(Decimal/int/date/datetime) are rendered from server-owned values whose
string forms can only match the number/date grammar (a leading ``-`` on
a genuine negative amount is a number, not a formula), so they are
emitted canonically and never corrupted by the prefix.

The PDF renderer is a deliberately minimal, dependency-free PDF 1.4
writer (Helvetica text pages). Adding a rendering library would need an
ADR (MASTER_PROMPT section 6); the report documents are tabular text,
which this covers without a new supply-chain surface. PDF text strings
are escaped per the PDF string grammar, so cell content can never break
out of its content stream.
"""

from __future__ import annotations

import csv
import io
from datetime import date, datetime
from decimal import Decimal

#: One cell of a rendered report row.
type Cell = str | Decimal | int | date | datetime | None

#: First characters a spreadsheet may interpret as a formula (OWASP).
_FORMULA_CHARS = frozenset("=+-@")

#: Prefix characters attackers use to hide a formula char from naive
#: "first character" checks; spreadsheets strip them on import.
_HIDING_PREFIX = frozenset("\t\r\n")


def escape_csv_text(value: str) -> str:
    """Neutralise spreadsheet formula injection in one text cell.

    A cell is dangerous when its first character AFTER any tab/CR/LF
    hiding prefix is one of ``= + - @``. Such cells are prefixed with a
    single quote, which spreadsheet applications render as literal
    text. Quoting of separators/quotes is handled by the csv writer.
    """
    stripped = value.lstrip("".join(_HIDING_PREFIX))
    if stripped and stripped[0] in _FORMULA_CHARS:
        return f"'{value}"
    return value


def format_cell(cell: Cell) -> str:
    """Canonical string form of a typed cell; text goes through escaping."""
    if cell is None:
        return ""
    if isinstance(cell, str):
        return escape_csv_text(cell)
    if isinstance(cell, datetime | date):
        return cell.isoformat()
    return str(cell)


class CsvBuilder:
    """Incremental CSV assembly, one batch of rows at a time.

    add_rows is pure CPU and is called by the export engine off the
    event loop (gate 1.3); state is sequential per export job.
    """

    def __init__(self, headers: tuple[str, ...]) -> None:
        self._buffer = io.StringIO()
        self._writer = csv.writer(self._buffer, lineterminator="\r\n")
        self._writer.writerow([escape_csv_text(h) for h in headers])

    def add_rows(self, rows: list[tuple[Cell, ...]]) -> None:
        for row in rows:
            self._writer.writerow([format_cell(cell) for cell in row])

    def finish(self) -> bytes:
        return self._buffer.getvalue().encode("utf-8")


# ---------------------------------------------------------------------------
# Minimal PDF 1.4 writer
# ---------------------------------------------------------------------------

_PAGE_WIDTH = 842  # A4 landscape, points
_PAGE_HEIGHT = 595
_MARGIN = 40
_LEAD = 13  # line height
_FONT_SIZE = 8
_TITLE_SIZE = 12
_LINES_PER_PAGE = (_PAGE_HEIGHT - 2 * _MARGIN) // _LEAD - 3
#: Reports wider than this render record-style (one "key: value" line
#: per column) instead of one row per line.
_WIDE_REPORT_COLUMNS = 8
_MAX_LINE_CHARS = 150


def _pdf_escape(value: str) -> str:
    """Escape a string for a PDF literal string ((), \\ and non-latin-1)."""
    encoded = value.encode("latin-1", errors="replace").decode("latin-1")
    return encoded.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")


def _row_lines(headers: tuple[str, ...], row: tuple[Cell, ...]) -> list[str]:
    cells = [format_cell(cell) for cell in row]
    if len(headers) > _WIDE_REPORT_COLUMNS:
        lines = [f"{h}: {c}" for h, c in zip(headers, cells, strict=True)]
        lines.append("")  # blank separator between records
        return lines
    return [" | ".join(cells)[:_MAX_LINE_CHARS]]


def _page_stream(lines: list[str], *, title: str, meta: str) -> bytes:
    parts = [
        "BT",
        f"/F1 {_TITLE_SIZE} Tf",
        f"1 0 0 1 {_MARGIN} {_PAGE_HEIGHT - _MARGIN} Tm",
        f"({_pdf_escape(title)}) Tj",
        f"/F1 {_FONT_SIZE} Tf",
        f"1 0 0 1 {_MARGIN} {_PAGE_HEIGHT - _MARGIN - 2 * _LEAD} Tm",
        f"{_LEAD} TL",
        f"({_pdf_escape(meta)}) Tj",
        "T*",
    ]
    for line in lines:
        parts.append(f"({_pdf_escape(line)}) Tj")
        parts.append("T*")
    parts.append("ET")
    return "\n".join(parts).encode("latin-1")


class PdfBuilder:
    """Incremental PDF assembly, one batch of rows at a time (DSA-4).

    The page model is line-oriented, so each batch is folded into its
    formatted LINES immediately and the raw Cell tuples are released
    with the batch — the export engine no longer accumulates rows for
    the PDF (P13.17d: the third in-memory copy is gone; what remains
    is one string per rendered line, the same order of memory as the
    CSV buffer). Page streams are assembled at finish(), because the
    per-page meta line (row count / truncation) is only known once the
    engine has observed the final batch.

    add_rows is pure CPU and is called by the export engine off the
    event loop (gate 1.3); state is sequential per export job.
    Batching is invisible in the output: any split of the same rows
    yields byte-identical PDFs (the FM4 equality gate,
    tests/test_p1317_pdf_incremental.py).
    """

    def __init__(self, headers: tuple[str, ...]) -> None:
        self._headers = headers
        self._lines: list[str] = []
        if len(headers) <= _WIDE_REPORT_COLUMNS:
            self._lines.append(" | ".join(headers)[:_MAX_LINE_CHARS])
        self._row_count = 0

    def add_rows(self, rows: list[tuple[Cell, ...]]) -> None:
        for row in rows:
            self._lines.extend(_row_lines(self._headers, row))
        self._row_count += len(rows)

    def finish(self, *, title: str, meta: str) -> bytes:
        lines = self._lines
        if self._row_count == 0:
            lines = [*lines, "(no rows)"]
        return _assemble_pdf(lines, title=title, meta=meta)


def render_pdf(
    *,
    title: str,
    meta: str,
    headers: tuple[str, ...],
    rows: list[tuple[Cell, ...]],
) -> bytes:
    """Render a tabular report as a paginated text PDF (pure CPU).

    One-shot convenience over PdfBuilder — the single source of truth
    for the page model (gate 1.1): a one-shot render IS an incremental
    render fed one batch.
    """
    builder = PdfBuilder(headers)
    builder.add_rows(rows)
    return builder.finish(title=title, meta=meta)


def _assemble_pdf(lines: list[str], *, title: str, meta: str) -> bytes:
    """Paginate formatted lines and emit the PDF 1.4 byte stream."""
    pages = [lines[i : i + _LINES_PER_PAGE] for i in range(0, len(lines), _LINES_PER_PAGE)]

    # Object layout: 1 catalog, 2 pages root, 3 font, then per page
    # (page object, content stream) pairs.
    objects: list[bytes] = []
    page_object_ids = [4 + 2 * i for i in range(len(pages))]
    kids = " ".join(f"{oid} 0 R" for oid in page_object_ids)
    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objects.append(f"<< /Type /Pages /Kids [{kids}] /Count {len(pages)} >>".encode("latin-1"))
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    for i, page_lines in enumerate(pages):
        stream = _page_stream(page_lines, title=title, meta=meta)
        page = (
            f"<< /Type /Page /Parent 2 0 R "
            f"/MediaBox [0 0 {_PAGE_WIDTH} {_PAGE_HEIGHT}] "
            f"/Resources << /Font << /F1 3 0 R >> >> "
            f"/Contents {page_object_ids[i] + 1} 0 R >>"
        ).encode("latin-1")
        objects.append(page)
        objects.append(
            f"<< /Length {len(stream)} >>\nstream\n".encode("latin-1") + stream + b"\nendstream"
        )

    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets: list[int] = []
    for number, body in enumerate(objects, start=1):
        offsets.append(out.tell())
        out.write(f"{number} 0 obj\n".encode("latin-1"))
        out.write(body)
        out.write(b"\nendobj\n")
    xref_at = out.tell()
    out.write(f"xref\n0 {len(objects) + 1}\n".encode("latin-1"))
    out.write(b"0000000000 65535 f \n")
    for offset in offsets:
        out.write(f"{offset:010d} 00000 n \n".encode("latin-1"))
    out.write(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref_at}\n%%EOF\n".encode("latin-1")
    )
    return out.getvalue()
