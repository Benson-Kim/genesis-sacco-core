"""Shared DB-backed helpers for the P13 export test suite (not a test module)."""

import uuid
from functools import partial

from sqlalchemy import text

from db_helpers import api_client, factory, seed_user, unique_email
from genesis.application.auth import AuthContext, issue_access_token
from genesis.application.exports import ExportCycleSummary, run_pending_exports
from genesis.application.rbac import seed_permissions
from genesis.infrastructure.tenancy import tenant_session, tenant_snapshot_session


async def seed_actor(role_name: str = "System Admin") -> tuple[uuid.UUID, uuid.UUID, str]:
    """Tenant + seeded permissions + bearer token; returns (tid, user_id, token)."""
    email = unique_email()
    tid, role_id = await seed_user(email, role_name=role_name)
    async with tenant_session(factory(), tid) as session:
        await seed_permissions(session, tid)
        user_id = (
            await session.execute(
                text("SELECT id FROM users WHERE email = :email"), {"email": email}
            )
        ).scalar_one()
    uid = uuid.UUID(str(user_id))
    token = issue_access_token(AuthContext(user_id=uid, tenant_id=tid, role_id=role_id))
    return tid, uid, token


async def add_user(tid: uuid.UUID, role_name: str) -> tuple[uuid.UUID, str]:
    """Extra user in an existing tenant (roles were seeded by seed_actor)."""
    user_id = uuid.uuid4()
    async with tenant_session(factory(), tid) as session:
        role_id = (
            await session.execute(
                text("SELECT id FROM roles WHERE name = :name"), {"name": role_name}
            )
        ).scalar_one()
        await session.execute(
            text(
                "INSERT INTO users (id, tenant_id, role_id, full_name, email) "
                "VALUES (CAST(:id AS uuid), CAST(:tid AS uuid), CAST(:rid AS uuid), "
                "'Extra User', :email)"
            ),
            {
                "id": str(user_id),
                "tid": str(tid),
                "rid": str(role_id),
                "email": unique_email(),
            },
        )
        rid = uuid.UUID(str(role_id))
    token = issue_access_token(AuthContext(user_id=user_id, tenant_id=tid, role_id=rid))
    return user_id, token


async def create_branch(token: str, name: str) -> str:
    """Create a branch through the real API; returns the branch id.

    Shared by the roster suites and the remediation-N1 boundary suite
    (review R1 on !83 — never a private cross-suite import, the F5
    lesson).
    """
    async with api_client() as client:
        res = await client.post(
            "/branches",
            json={"name": name},
            headers={"authorization": f"Bearer {token}"},
        )
        assert res.status_code == 201, res.text
        branch_id: str = res.json()["id"]
    return branch_id


async def seed_member(
    tid: uuid.UUID,
    *,
    name: str = "Report Member",
    deposit: str = "0",
    shares: str = "0",
) -> uuid.UUID:
    mid = uuid.uuid4()
    async with tenant_session(factory(), tid) as session:
        await session.execute(
            text(
                "INSERT INTO members (id, tenant_id, member_no, type, name) VALUES "
                "(CAST(:id AS uuid), CAST(:tid AS uuid), :no, 'person', :name)"
            ),
            {"id": str(mid), "tid": str(tid), "no": f"GP-{mid.hex[:6].upper()}", "name": name},
        )
        for table, bal in (("deposit_accounts", deposit), ("share_accounts", shares)):
            await session.execute(
                text(
                    # Table name from test code, never user input.
                    f"INSERT INTO {table} (id, tenant_id, member_id, balance) VALUES "  # noqa: S608
                    "(CAST(:id AS uuid), CAST(:tid AS uuid), CAST(:m AS uuid), :bal)"
                ),
                {"id": str(uuid.uuid4()), "tid": str(tid), "m": str(mid), "bal": bal},
            )
    return mid


async def seed_member_no(
    tid: uuid.UUID,
    member_no: str,
    *,
    name: str,
    status: str = "active",
    branch_id: str | None = None,
) -> uuid.UUID:
    """Member with a CONTROLLED member_no so keyset order is hand-computable.

    Shared by the roster suites and the remediation-N1 boundary suite
    (review R1 on !83 — never a private cross-suite import, the F5
    lesson).
    """
    mid = uuid.uuid4()
    async with tenant_session(factory(), tid) as session:
        await session.execute(
            text(
                "INSERT INTO members "
                "(id, tenant_id, member_no, type, name, status, branch_id) "
                "VALUES (CAST(:id AS uuid), CAST(:tid AS uuid), :no, 'person', "
                ":name, :status, CAST(:bid AS uuid))"
            ),
            {
                "id": str(mid),
                "tid": str(tid),
                "no": member_no,
                "name": name,
                "status": status,
                "bid": branch_id,
            },
        )
    return mid


async def seed_three_leg_repayment(tid: uuid.UUID, mid: uuid.UUID) -> uuid.UUID:
    """A balanced 3-leg posting with hand-computed figures (#31 (g)).

    100.00 repayment split 60.00 principal + 40.00 interest:
      DR cash.mpesa        100.00
      CR income.interest    40.00
      CR loans.receivable   60.00
    Balanced AND equal to transactions.amount, so the deferred 0004/
    0014 trigger accepts the commit — the same shape the P10 allocated
    repayment writes. Deliberately NON-ADDITIVE-looking figures (no
    leg equals the sum of the others' complement pattern a client
    might "helpfully" total). Shared by the legs read suite and the
    batch-7 EXPLAIN capture (never a private cross-suite import).
    """
    txn_id = uuid.uuid4()
    async with tenant_session(factory(), tid) as session:
        await session.execute(
            text(
                "INSERT INTO transactions "
                "(id, tenant_id, txn_ref, member_id, type, amount, channel) "
                "VALUES (CAST(:id AS uuid), CAST(:tid AS uuid), :ref, "
                "CAST(:mid AS uuid), 'loan_repayment', 100.00, 'mpesa')"
            ),
            {
                "id": str(txn_id),
                "tid": str(tid),
                "mid": str(mid),
                "ref": f"RP-{txn_id.hex[:8]}",
            },
        )
        await session.execute(
            text(
                "INSERT INTO ledger_entries "
                "(id, tenant_id, transaction_id, account, side, amount) "
                "VALUES "
                "(CAST(:dr AS uuid), CAST(:tid AS uuid), "
                "CAST(:txn AS uuid), 'cash.mpesa', 'debit', 100.00), "
                "(CAST(:cr1 AS uuid), CAST(:tid AS uuid), "
                "CAST(:txn AS uuid), 'income.interest', 'credit', 40.00), "
                "(CAST(:cr2 AS uuid), CAST(:tid AS uuid), "
                "CAST(:txn AS uuid), 'loans.receivable', 'credit', 60.00)"
            ),
            {
                "dr": str(uuid.uuid4()),
                "cr1": str(uuid.uuid4()),
                "cr2": str(uuid.uuid4()),
                "tid": str(tid),
                "txn": str(txn_id),
            },
        )
    return txn_id


async def drain_export_queue(tid: uuid.UUID) -> ExportCycleSummary:
    """Run the real worker path: snapshot transactions per job."""
    return await run_pending_exports(partial(tenant_snapshot_session, factory(), tid), tid)


async def count(tid: uuid.UUID, sql: str, **params: object) -> int:
    async with tenant_session(factory(), tid) as session:
        value = (await session.execute(text(sql), params)).scalar_one()
    return int(value)
