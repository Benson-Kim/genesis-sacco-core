"""FM9 — per-endpoint cursor tamper legs (#31 batch 13, review N4).

ONE shared parametrized suite over every swept list endpoint reachable
with cheap seeding (one admin, one member, one branch): each endpoint
must refuse BOTH

* a garbage cursor (proves the endpoint routes its cursor through the
  codec's strict decode at all), and
* a VALIDLY SIGNED token minted for a DIFFERENT endpoint scope in the
  SAME tenant (the strong leg: proves the per-endpoint scope binding —
  falsifiable per endpoint by serving/accepting plaintext or by
  dropping the endpoint identity from the MAC),

with the sanitized 400 envelope (category + correlation_id ONLY —
never a 500, never a silently empty 200 page, no internals echoed).

Deep-parent endpoints reuse their module seeders instead of this file
(the F5 lesson — no private cross-suite imports): recovery-case notes
in test_recovery_cases.py, write-off recovery receipts in
test_loan_recoveries.py. The dividends-module endpoints
(/dividends/declarations, /share-transfers) joined this matrix when
the batch-12 gate cleared (!85 merged 2026-08-08; review B13-R7).
"""

import asyncio
import importlib
import os
import pkgutil
import uuid

import pytest

from db_helpers import api_client
from export_helpers import create_branch, seed_actor, seed_member_no
from genesis import application as application_pkg
from genesis.application.accounting_periods import PERIODS_LIST_SCOPE
from genesis.application.audit_log import AUDIT_LIST_SCOPE
from genesis.application.branches import (
    BRANCH_MEMBERS_SCOPE,
    BRANCH_USERS_SCOPE,
    BRANCHES_LIST_SCOPE,
)
from genesis.application.corrections import (
    ADJUSTMENTS_SCOPE,
    WO_RECOVERIES_SCOPE,
    WRITE_OFFS_SCOPE,
)
from genesis.application.dividends import DECLARATIONS_LIST_SCOPE, SHARE_TRANSFERS_SCOPE
from genesis.application.loan_applications import APPLICATIONS_LIST_SCOPE
from genesis.application.loans import LOAN_BOOK_SCOPE
from genesis.application.member_exits import EXITS_LIST_SCOPE
from genesis.application.member_kyc import DOCUMENTS_LIST_SCOPE
from genesis.application.members import MEMBERS_LIST_SCOPE, STATEMENT_SCOPE
from genesis.application.pagination import encode_cursor
from genesis.application.recovery import CASE_NOTES_SCOPE, WORKLIST_SCOPE
from genesis.application.transactions import TXN_LIST_SCOPE
from genesis.application.users import USERS_LIST_SCOPE

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="requires a migrated database"
)

#: A plausible plaintext keyset position — validly signed below under a
#: scope NO endpoint uses, so every decode must refuse it by tag.
_FOREIGN_SCOPE = "tamper.test"
_PAYLOAD = f"2026-01-01T00:00:00+00:00|{uuid.uuid4()}"

#: (endpoint template, human id, exported scope symbol) — the SAME
#: code-owned constants the application decode sites use (B13-R2:
#: never re-typed literals). Templates take {mid} / {bid}. The scope
#: column feeds the FM9 totality leg below.
ENDPOINTS = [
    ("/members", "members plain", MEMBERS_LIST_SCOPE),
    ("/members?include=aggregates", "members aggregates", MEMBERS_LIST_SCOPE),
    ("/members/{mid}/statement", "member statement", STATEMENT_SCOPE),
    ("/members/{mid}/documents", "kyc documents", DOCUMENTS_LIST_SCOPE),
    ("/branches", "branches", BRANCHES_LIST_SCOPE),
    ("/branches/{bid}/users", "branch user roster", BRANCH_USERS_SCOPE),
    ("/branches/{bid}/members", "branch member roster", BRANCH_MEMBERS_SCOPE),
    ("/accounting-periods", "accounting periods", PERIODS_LIST_SCOPE),
    ("/audit-log", "audit log", AUDIT_LIST_SCOPE),
    ("/loans", "loan book", LOAN_BOOK_SCOPE),
    ("/applications", "loan applications", APPLICATIONS_LIST_SCOPE),
    ("/member-exits", "member exits", EXITS_LIST_SCOPE),
    ("/transactions", "transactions", TXN_LIST_SCOPE),
    ("/users", "users", USERS_LIST_SCOPE),
    ("/recovery-cases", "recovery worklist", WORKLIST_SCOPE),
    ("/corrections/repayment-adjustments", "adjustments register", ADJUSTMENTS_SCOPE),
    ("/corrections/write-offs", "write-off register", WRITE_OFFS_SCOPE),
    ("/dividends/declarations", "dividend declarations", DECLARATIONS_LIST_SCOPE),
    ("/share-transfers", "share transfers", SHARE_TRANSFERS_SCOPE),
]

#: Scopes whose tamper legs live next to their module seeders (deep
#: parents reuse their own fixtures — the F5 lesson, no private
#: cross-suite imports). Keyed by the SAME exported constants.
DELEGATED = {
    CASE_NOTES_SCOPE: (
        "test_recovery_cases.py::test_notes_append_only_keyset_and_closed_case_refusal"
    ),
    WO_RECOVERIES_SCOPE: (
        "test_loan_recoveries.py::test_receipts_listing_pages_by_keyset_with_reconstructed_totals"
    ),
}

#: Seeded once, shared across the parametrized legs (ids and the JWT
#: are loop-independent strings; the DB row set is session-shared).
_CTX: dict[str, str] = {}


async def _ctx() -> dict[str, str]:
    if not _CTX:
        tid, _, token = await seed_actor()
        branch_id = await create_branch(token, f"T-{uuid.uuid4().hex[:8]}")
        member_id = await seed_member_no(tid, "GP-0001", name="Tamper Probe")
        _CTX.update(tid=str(tid), token=token, branch_id=branch_id, member_id=str(member_id))
    return _CTX


@pytest.mark.parametrize(("template", "label", "scope"), ENDPOINTS, ids=[e[1] for e in ENDPOINTS])
def test_forged_cursor_is_a_sanitized_400(template: str, label: str, scope: str) -> None:
    async def run() -> None:
        ctx = await _ctx()
        path = template.format(mid=ctx["member_id"], bid=ctx["branch_id"])
        # Cross-scope replay: correctly signed for the SAME tenant but
        # a scope no endpoint uses — only the tag check can refuse it.
        cross_scope = encode_cursor(
            _PAYLOAD, tenant_id=uuid.UUID(ctx["tid"]), endpoint=_FOREIGN_SCOPE
        )
        # And a token validly signed for a DIFFERENT REAL endpoint's
        # scope (B13-R2: distinct scopes must never interchange).
        other_real = AUDIT_LIST_SCOPE if scope != AUDIT_LIST_SCOPE else MEMBERS_LIST_SCOPE
        cross_real = encode_cursor(_PAYLOAD, tenant_id=uuid.UUID(ctx["tid"]), endpoint=other_real)
        headers = {"authorization": f"Bearer {ctx['token']}"}
        sep = "&" if "?" in path else "?"
        async with api_client() as client:
            for forged in ("not-a-cursor", cross_scope, cross_real):
                res = await client.get(f"{path}{sep}cursor={forged}", headers=headers)
                assert res.status_code == 400, (label, forged, res.status_code, res.text)
                body = res.json()
                assert set(body.keys()) == {"category", "correlation_id"}, (label, body)
                assert body["category"] == "validation_error", (label, body)

    asyncio.run(run())


def _exported_scope_constants() -> dict[str, str]:
    """Every exported cursor-scope constant in the application package
    (qualified name -> scope id), discovered by a module walk so a NEW
    list endpoint's scope cannot ship without joining this matrix."""
    found: dict[str, str] = {}
    for info in pkgutil.iter_modules(application_pkg.__path__):
        module = importlib.import_module(f"genesis.application.{info.name}")
        for name, value in vars(module).items():
            if name.endswith("_SCOPE") and not name.startswith("_") and isinstance(value, str):
                found[f"{info.name}.{name}"] = value
    return found


def test_scope_ids_are_pairwise_distinct() -> None:
    """B13-R2 leg (a): cross-endpoint replay must never silently share
    a scope — every exported scope id is pairwise DISTINCT, and none
    collides with this suite's deliberately foreign scope.
    Falsifiable: point two endpoints at one scope id and this fails."""
    inventory = _exported_scope_constants()
    values = list(inventory.values())
    assert len(values) == len(set(values)), inventory
    assert _FOREIGN_SCOPE not in set(values)


def test_fm9_totality_every_scope_constant_has_a_tamper_leg() -> None:
    """B13-R2 leg (b): FM9 totality — every inventoried listing shape's
    scope constant is exercised by the parametrized tamper matrix,
    either in ENDPOINTS here or by a named delegated module leg.
    Falsifiable in BOTH directions: a new exported *_SCOPE constant
    without a tamper leg fails, and a stale ENDPOINTS/DELEGATED entry
    naming a retired scope fails."""
    inventory = set(_exported_scope_constants().values())
    covered = {scope for _, _, scope in ENDPOINTS} | set(DELEGATED)
    assert covered == inventory, (sorted(covered), sorted(inventory))
