"""Pure rendering tests: CSV formula-injection escaping + PDF (P13 b).

Every oracle below is hand-computed in comments — never captured from
the renderer under test.
"""

import csv
import io
from datetime import UTC, date, datetime
from decimal import Decimal

from genesis.domain.documents import (
    CsvBuilder,
    escape_csv_text,
    format_cell,
    render_pdf,
)

HYPERLINK_NAME = '=HYPERLINK("http://evil.example/x","Open")'


# ---------------------------------------------------------------------------
# escape_csv_text — the formula-injection guard (P13 blocker b)
# ---------------------------------------------------------------------------


def test_formula_characters_are_prefixed() -> None:
    # Hand-computed: each dangerous first char gains a leading quote.
    assert escape_csv_text("=1+1") == "'=1+1"
    assert escape_csv_text("+254700000000") == "'+254700000000"
    assert escape_csv_text("-2+3") == "'-2+3"
    assert escape_csv_text("@SUM(A1)") == "'@SUM(A1)"
    assert escape_csv_text(HYPERLINK_NAME) == f"'{HYPERLINK_NAME}"


def test_tab_and_cr_hidden_variants_are_prefixed() -> None:
    # Spreadsheets strip leading tab/CR/LF on import, so "\t=1+1" is as
    # dangerous as "=1+1"; the guard must look past the hiding prefix
    # while preserving the original value.
    assert escape_csv_text("\t=1+1") == "'\t=1+1"
    assert escape_csv_text("\r=1+1") == "'\r=1+1"
    assert escape_csv_text("\n=1+1") == "'\n=1+1"
    assert escape_csv_text("\r\n\t=cmd") == "'\r\n\t=cmd"


def test_benign_text_is_unchanged() -> None:
    assert escape_csv_text("Jane Wanjiku") == "Jane Wanjiku"
    assert escape_csv_text("GP-0001") == "GP-0001"
    assert escape_csv_text("") == ""
    # Whitespace-only content has no first character to weaponise.
    assert escape_csv_text("\t") == "\t"


def test_typed_cells_render_canonically() -> None:
    # A negative Decimal is a genuine number whose string form can only
    # match the number grammar — it is NOT prefixed (documented in
    # genesis.domain.documents); free-text cells are the injection
    # surface and always go through the guard.
    assert format_cell(Decimal("-100.00")) == "-100.00"
    assert format_cell(Decimal("150000.50")) == "150000.50"
    assert format_cell(None) == ""
    assert format_cell(7) == "7"
    assert format_cell(date(2026, 7, 29)) == "2026-07-29"
    assert format_cell(datetime(2026, 7, 29, 12, 0, tzinfo=UTC)) == "2026-07-29T12:00:00+00:00"


# ---------------------------------------------------------------------------
# CsvBuilder — mandatory inert-cell test (P13 blocker b)
# ---------------------------------------------------------------------------


def test_csv_member_named_hyperlink_is_inert() -> None:
    """MANDATORY (P13 blocker b): a member named '=HYPERLINK(...)'
    round-trips as inert text, never as a formula."""
    builder = CsvBuilder(("Member No", "Member", "Balance"))
    builder.add_rows([("GP-0001", HYPERLINK_NAME, Decimal("100.00"))])
    payload = builder.finish().decode("utf-8")

    parsed = list(csv.reader(io.StringIO(payload)))
    # Hand-computed layout: header row + one data row.
    assert parsed[0] == ["Member No", "Member", "Balance"]
    cell = parsed[1][1]
    # The emitted cell is the quoted-prefix form: a spreadsheet renders
    # it as literal text. Removing escape_csv_text from the renderer
    # makes cell == HYPERLINK_NAME and this test fails (falsifiable).
    assert cell == f"'{HYPERLINK_NAME}"
    assert not cell.startswith("=")


def test_csv_hand_computed_bytes() -> None:
    builder = CsvBuilder(("A", "B"))
    builder.add_rows([("plain", Decimal("1.00")), ('say "hi"', None)])
    # Hand-computed: csv module doubles embedded quotes and quotes the
    # cell; CRLF line endings per RFC 4180.
    assert builder.finish() == b'A,B\r\nplain,1.00\r\n"say ""hi""",\r\n'


# ---------------------------------------------------------------------------
# render_pdf
# ---------------------------------------------------------------------------


def test_pdf_structure_and_escaping() -> None:
    pdf = render_pdf(
        title="Trial balance",
        meta="As of 2026-07-29",
        headers=("Account", "Debits", "Credits"),
        rows=[("cash (bank)", Decimal("10.00"), Decimal("0"))],
    )
    assert pdf.startswith(b"%PDF-1.4")
    assert pdf.rstrip().endswith(b"%%EOF")
    # Parentheses in cell text must be escaped per the PDF string
    # grammar or the content stream would be corrupted (falsifiable:
    # remove _pdf_escape and this raw form appears).
    assert rb"cash \(bank\)" in pdf
    assert b"(cash (bank)" not in pdf


def test_pdf_paginates_long_reports() -> None:
    rows = [(f"row-{i}", Decimal("1.00")) for i in range(200)]
    pdf = render_pdf(title="T", meta="m", headers=("A", "B"), rows=rows)
    # Hand-computed: 39 lines/page (A4 landscape, 13pt lead, margins,
    # 3 header lines) -> 201 lines (header + 200 rows) need 6 pages.
    assert pdf.count(b"/Type /Page ") == 6
    assert b"/Count 6" in pdf


def test_pdf_wide_reports_render_record_style() -> None:
    headers = tuple(f"H{i}" for i in range(10))  # > 8 columns
    pdf = render_pdf(
        title="Wide",
        meta="m",
        headers=headers,
        rows=[tuple(f"v{i}" for i in range(10))],
    )
    assert b"(H3: v3) Tj" in pdf
