"""Structural proof for the member-exits SQL composition (#36).

member_exits interpolates exactly two structural fragments into SQL —
the code-owned projection constants (a column list can never be a
bound parameter). These pure string tests make the single audited
composition site's two remaining ``noqa: S608`` suppressions
falsifiable instead of trust-me:

  * charset lock — the interpolated constants contain only identifier
    characters: no quote, no semicolon, no placeholder, no parenthesis
    can ever ride them into a statement;
  * duplication lock — no column appears twice (the #24 regression
    class: a silently duplicated column shifts every index-based read
    after it);
  * arity lock — the projection width matches the positional
    ``_row_to_exit`` / ``ExitRecord`` mapping, so an added or removed
    column fails HERE, not as a shifted read at runtime;
  * byte-for-byte lock — the composed prefixes equal exactly the
    statements the pre-#36 f-strings produced (zero behavior change);
  * value-parameter lock — the open-application stage probe binds its
    stages as parameters, and the placeholder list can never diverge
    from the bound map (they share one code-owned tuple).

No database required: every assertion is on module-level constants.
"""

from __future__ import annotations

import dataclasses
import re

from genesis.application.member_exits import (
    _EXIT_COLS,
    _EXIT_LABEL_JOIN,
    _EXIT_READ_COLS,
    _EXIT_READ_SELECT,
    _EXIT_SELECT,
    _OPEN_STAGES,
    OPEN_APPLICATIONS_SQL,
    ExitRecord,
    open_stage_params,
)

#: The exact statement prefix the pre-#36 f-string produced for the
#: settlement's join-free FOR UPDATE read (captured before the
#: refactor; the composed constant must never drift from it).
_GOLDEN_SETTLEMENT_PREFIX = (
    "SELECT id, member_id, status, reason, shares_amount, deposits_amount, "
    "loan_balance, fees, net_payable, requested_by, decided_at, settled_at, "
    "settlement_transaction_id, version, created_at, exit_ref "
    "FROM member_exits "
)

#: The exact statement prefix the pre-#36 f-strings produced for the
#: labelled (un-locked) reads: table-qualified projection + label join.
_GOLDEN_READ_PREFIX = (
    "SELECT member_exits.id, member_exits.member_id, member_exits.status, "
    "member_exits.reason, member_exits.shares_amount, "
    "member_exits.deposits_amount, member_exits.loan_balance, "
    "member_exits.fees, member_exits.net_payable, "
    "member_exits.requested_by, member_exits.decided_at, "
    "member_exits.settled_at, member_exits.settlement_transaction_id, "
    "member_exits.version, member_exits.created_at, member_exits.exit_ref, "
    "mm.member_no, mm.name "
    "FROM member_exits "
    "LEFT JOIN members mm ON mm.tenant_id = member_exits.tenant_id "
    "AND mm.id = member_exits.member_id "
)


def test_projection_constants_are_identifier_charset_only() -> None:
    # The falsifiable proof behind the two S608 suppressions: nothing
    # but [A-Za-z_., ] can ride the interpolated projections into SQL.
    assert re.fullmatch(r"[A-Za-z_., ]+", _EXIT_COLS)
    assert re.fullmatch(r"[A-Za-z_., ]+", _EXIT_READ_COLS)
    # The label join additionally needs '=' for its ON conditions.
    assert re.fullmatch(r"[A-Za-z_.= ]+", _EXIT_LABEL_JOIN)


def test_projections_contain_no_duplicate_column() -> None:
    cols = _EXIT_COLS.split(", ")
    read_cols = _EXIT_READ_COLS.split(", ")
    assert len(cols) == len(set(cols))
    assert len(read_cols) == len(set(read_cols))


def test_projection_arity_matches_exit_record() -> None:
    fields = dataclasses.fields(ExitRecord)
    # The labelled read supplies every ExitRecord field positionally;
    # the join-free settlement read supplies all but the two trailing,
    # defaulted display labels.
    assert [f.name for f in fields[-2:]] == ["member_no", "member_name"]
    assert len(_EXIT_READ_COLS.split(", ")) == len(fields)
    assert len(_EXIT_COLS.split(", ")) == len(fields) - 2


def test_composed_prefixes_are_byte_locked_to_pre_refactor_sql() -> None:
    assert _EXIT_SELECT == _GOLDEN_SETTLEMENT_PREFIX
    assert _EXIT_READ_SELECT == _GOLDEN_READ_PREFIX


def test_open_application_probe_binds_stages_as_parameters() -> None:
    params = open_stage_params()
    placeholders = re.findall(r":(stage\d+)", OPEN_APPLICATIONS_SQL)
    # Placeholders and bound map in lockstep, in tuple order.
    assert placeholders == list(params)
    assert list(params.values()) == list(_OPEN_STAGES)
    # The P8 blocker set itself (single list, locked).
    assert _OPEN_STAGES == ("submitted", "appraisal", "committee", "approved")
    # No stage value leaks into the statement text.
    for stage in _OPEN_STAGES:
        assert stage not in OPEN_APPLICATIONS_SQL
