"""Loan product services (P9, settings module).

Products carry the pricing rules (rate, deposit multiplier, max term)
that applications must obey. Edits are optimistic-locked (concurrency safety) and
audited in-transaction (data integrity).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, cast

from sqlalchemy import CursorResult, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from genesis.application.audit import record_audit
from genesis.errors import ConflictError, NotFoundError

_COLS = (
    "id, name, rate_pct, deposit_multiplier, max_term_months, guarantors_required, active, version"
)


@dataclass(frozen=True)
class LoanProduct:
    id: uuid.UUID
    name: str
    rate_pct: Decimal
    deposit_multiplier: Decimal
    max_term_months: int
    #:  stored configuration (prototype Settings > Loan products);
    #: disbursement-time enforcement is a recorded follow-up.
    guarantors_required: int
    active: bool
    version: int


def _row_to_product(row: Any) -> LoanProduct:
    return LoanProduct(
        id=uuid.UUID(str(row[0])),
        name=str(row[1]),
        rate_pct=Decimal(str(row[2])),
        deposit_multiplier=Decimal(str(row[3])),
        max_term_months=int(row[4]),
        guarantors_required=int(row[5]),
        active=bool(row[6]),
        version=int(row[7]),
    )


async def create_product(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID | None,
    *,
    name: str,
    rate_pct: Decimal,
    deposit_multiplier: Decimal,
    max_term_months: int,
    guarantors_required: int = 0,
) -> LoanProduct:
    product_id = uuid.uuid4()
    try:
        await session.execute(
            text(
                "INSERT INTO loan_products "
                "(id, tenant_id, name, rate_pct, deposit_multiplier, max_term_months, "
                " guarantors_required) "
                "VALUES (CAST(:id AS uuid), CAST(:tid AS uuid), :name, "
                ":rate, :mult, :term, :guarantors)"
            ),
            {
                "id": str(product_id),
                "tid": str(tenant_id),
                "name": name,
                "rate": str(rate_pct),
                "mult": str(deposit_multiplier),
                "term": max_term_months,
                "guarantors": guarantors_required,
            },
        )
    except IntegrityError as exc:
        raise ConflictError(f"loan product '{name}' already exists") from exc
    await record_audit(
        session,
        tenant_id,
        actor_id,
        action="product.create",
        entity="loan_products",
        entity_id=str(product_id),
        after={
            "name": name,
            "rate_pct": str(rate_pct),
            "deposit_multiplier": str(deposit_multiplier),
            "max_term_months": max_term_months,
            "guarantors_required": guarantors_required,
        },
    )
    return LoanProduct(
        id=product_id,
        name=name,
        rate_pct=rate_pct,
        deposit_multiplier=deposit_multiplier,
        max_term_months=max_term_months,
        guarantors_required=guarantors_required,
        active=True,
        version=1,
    )


async def get_product(
    session: AsyncSession, tenant_id: uuid.UUID, product_id: uuid.UUID
) -> LoanProduct:
    # Explicit tenant predicate on top of RLS (defence in depth,
    # least disclosure v1.1).
    row = (
        await session.execute(
            text(
                f"SELECT {_COLS} FROM loan_products "  # noqa: S608
                "WHERE id = CAST(:id AS uuid) AND tenant_id = CAST(:tid AS uuid)"
            ),
            {"id": str(product_id), "tid": str(tenant_id)},
        )
    ).first()
    if row is None:
        raise NotFoundError(f"loan product {product_id} not found")
    return _row_to_product(row)


async def list_products(session: AsyncSession, tenant_id: uuid.UUID) -> list[LoanProduct]:
    rows = (
        await session.execute(
            text(
                f"SELECT {_COLS} FROM loan_products "  # noqa: S608
                "WHERE tenant_id = CAST(:tid AS uuid) ORDER BY name LIMIT 200"
            ),
            {"tid": str(tenant_id)},
        )
    ).all()
    return [_row_to_product(r) for r in rows]


async def update_product(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID | None,
    product_id: uuid.UUID,
    *,
    version: int,
    rate_pct: Decimal | None = None,
    deposit_multiplier: Decimal | None = None,
    max_term_months: int | None = None,
    guarantors_required: int | None = None,
    active: bool | None = None,
) -> LoanProduct:
    """Optimistic-locked product edit; stale version surfaces 409 (concurrency safety)."""
    current = await get_product(session, tenant_id, product_id)
    new_rate = rate_pct if rate_pct is not None else current.rate_pct
    new_mult = deposit_multiplier if deposit_multiplier is not None else current.deposit_multiplier
    new_term = max_term_months if max_term_months is not None else current.max_term_months
    new_guarantors = (
        guarantors_required if guarantors_required is not None else current.guarantors_required
    )
    new_active = active if active is not None else current.active
    result = cast(
        CursorResult[Any],
        await session.execute(
            text(
                # Explicit tenant predicate on the write, on top of RLS
                # (defence in depth, least disclosure v1.1).
                "UPDATE loan_products SET rate_pct = :rate, deposit_multiplier = :mult, "
                "max_term_months = :term, guarantors_required = :guarantors, "
                "active = :active, "
                "version = version + 1, updated_at = now() "
                "WHERE id = CAST(:id AS uuid) AND tenant_id = CAST(:tid AS uuid) "
                "AND version = :ver"
            ),
            {
                "rate": str(new_rate),
                "mult": str(new_mult),
                "term": new_term,
                "guarantors": new_guarantors,
                "active": new_active,
                "id": str(product_id),
                "tid": str(tenant_id),
                "ver": version,
            },
        ),
    )
    if result.rowcount != 1:
        raise ConflictError(f"stale version {version} for product {product_id}")
    await record_audit(
        session,
        tenant_id,
        actor_id,
        action="product.update",
        entity="loan_products",
        entity_id=str(product_id),
        before={
            "rate_pct": str(current.rate_pct),
            "deposit_multiplier": str(current.deposit_multiplier),
            "max_term_months": current.max_term_months,
            "guarantors_required": current.guarantors_required,
            "active": current.active,
            "version": current.version,
        },
        after={
            "rate_pct": str(new_rate),
            "deposit_multiplier": str(new_mult),
            "max_term_months": new_term,
            "guarantors_required": new_guarantors,
            "active": new_active,
            "version": current.version + 1,
        },
    )
    return LoanProduct(
        id=current.id,
        name=current.name,
        rate_pct=new_rate,
        deposit_multiplier=new_mult,
        max_term_months=new_term,
        guarantors_required=new_guarantors,
        active=new_active,
        version=current.version + 1,
    )
