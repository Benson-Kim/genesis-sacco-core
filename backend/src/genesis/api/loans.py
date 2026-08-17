"""Loan endpoints: products, applications, committee votes, guarantees (P9).

Every route carries a RequirePermission dependency (deny-by-default,
gate 1.6); mutations are idempotent via the Idempotency-Key middleware
(gate 1.4). APPROVED is only reachable through committee quorum and
DISBURSED only through the P7 disbursement contract.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field

from genesis.api.authz import RequireAnyPermission, RequirePermission
from genesis.application import guarantees as guarantees_service
from genesis.application import loan_applications as applications_service
from genesis.application import loan_products as products_service
from genesis.application.auth import AuthContext
from genesis.domain.committee import Vote
from genesis.domain.lending import ApplicationStage
from genesis.domain.rbac import Action, Module
from genesis.infrastructure.db import get_sessionmaker
from genesis.infrastructure.tenancy import tenant_session
from genesis.settings import get_settings

router = APIRouter(tags=["loans"])

_settings_view = RequirePermission(Module.SETTINGS, Action.VIEW)
_settings_create = RequirePermission(Module.SETTINGS, Action.CREATE)
_settings_edit = RequirePermission(Module.SETTINGS, Action.EDIT)
_apps_view = RequirePermission(Module.APPLICATIONS, Action.VIEW)
_apps_create = RequirePermission(Module.APPLICATIONS, Action.CREATE)
_apps_edit = RequirePermission(Module.APPLICATIONS, Action.EDIT)
_apps_approve = RequirePermission(Module.APPLICATIONS, Action.APPROVE)
#: Product discovery (external Codex review, re-derived): the
#: prototype's new-application form needs the product list, so
#: application creators (e.g. Loan Officer, who holds no settings
#: permissions in the P4 matrix) may read it alongside settings
#: viewers. Deny-by-default is preserved: the grant list is code-owned
#: and checked server-side; product WRITES stay settings-only.
_products_list = RequireAnyPermission(
    (Module.SETTINGS, Action.VIEW),
    (Module.APPLICATIONS, Action.CREATE),
)

SettingsViewCtx = Annotated[AuthContext, Depends(_settings_view)]
SettingsCreateCtx = Annotated[AuthContext, Depends(_settings_create)]
SettingsEditCtx = Annotated[AuthContext, Depends(_settings_edit)]
AppsViewCtx = Annotated[AuthContext, Depends(_apps_view)]
AppsCreateCtx = Annotated[AuthContext, Depends(_apps_create)]
AppsEditCtx = Annotated[AuthContext, Depends(_apps_edit)]
AppsApproveCtx = Annotated[AuthContext, Depends(_apps_approve)]
ProductListCtx = Annotated[AuthContext, Depends(_products_list)]


class ProductCreateBody(BaseModel):
    """extra="forbid" (gate 1.6 v1.1, retroactive on touched code).

    max_digits/decimal_places on the money-rule fields mirror the
    backing numeric(5,2) columns (0001), so an over-precise value is a
    clean 422 instead of a DB error surfacing as a 500 (external Codex
    review, re-derived).
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=100)
    rate_pct: Decimal = Field(gt=0, le=100, max_digits=5, decimal_places=2)
    deposit_multiplier: Decimal = Field(gt=0, max_digits=5, decimal_places=2)
    max_term_months: int = Field(ge=1, le=120)
    #: P13.7: stored product configuration (prototype Settings screen);
    #: bounds mirror the 0017 DB CHECK.
    guarantors_required: int = Field(default=0, ge=0, le=10)


class ProductUpdateBody(BaseModel):
    """Partial update: absent fields stay unchanged (the Codex version
    made rate_pct/deposit_multiplier mandatory, silently breaking
    partial updates — rejected; the numeric(5,2) alignment is kept)."""

    model_config = ConfigDict(extra="forbid")

    version: int = Field(ge=1)
    rate_pct: Decimal | None = Field(default=None, gt=0, le=100, max_digits=5, decimal_places=2)
    deposit_multiplier: Decimal | None = Field(default=None, gt=0, max_digits=5, decimal_places=2)
    max_term_months: int | None = Field(default=None, ge=1, le=120)
    guarantors_required: int | None = Field(default=None, ge=0, le=10)
    active: bool | None = None


class ProductOut(BaseModel):
    id: str
    name: str
    rate_pct: str
    deposit_multiplier: str
    max_term_months: int
    guarantors_required: int
    active: bool
    version: int


class ApplicationCreateBody(BaseModel):
    """extra="forbid": rate/pricing come from the product server-side;
    a caller-sent rate_pct (or any unknown field) is a 422 (gate 1.6
    v1.1, retroactive on touched code)."""

    model_config = ConfigDict(extra="forbid")

    member_id: uuid.UUID
    product_id: uuid.UUID
    amount: Decimal = Field(gt=0, le=1_000_000_000)
    term_months: int = Field(ge=1)
    purpose: str | None = Field(default=None, max_length=500)


class ApplicationOut(BaseModel):
    id: str
    member_id: str
    product_id: str
    amount: str
    term_months: int
    rate_pct: str
    purpose: str | None
    stage: str
    cover_pct: str
    #: deposits x product multiplier + live guarantees — the cap the P7
    #: disbursement gate enforces (issue #15). Computed on the single-
    #: application read only; None on listings (computing it per row
    #: would grow queries with result size, gate 1.3).
    max_eligible: str | None = None
    version: int


class ApplicationListResponse(BaseModel):
    items: list[ApplicationOut]
    next_cursor: str | None


class TransitionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int = Field(ge=1)
    target: ApplicationStage


class VoteBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    vote: Vote


class VoteResultOut(BaseModel):
    approvals: int
    rejections: int
    decision: str | None
    stage: str


class GuaranteePledgeBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    guarantor_member_id: uuid.UUID
    amount: Decimal = Field(gt=0, le=1_000_000_000)


class ConsentBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int = Field(ge=1)


class GuaranteeOut(BaseModel):
    id: str
    application_id: str
    guarantor_member_id: str
    borrower_member_id: str
    amount: str
    status: str
    version: int


def _product_out(p: products_service.LoanProduct) -> ProductOut:
    return ProductOut(
        id=str(p.id),
        name=p.name,
        rate_pct=str(p.rate_pct),
        deposit_multiplier=str(p.deposit_multiplier),
        max_term_months=p.max_term_months,
        guarantors_required=p.guarantors_required,
        active=p.active,
        version=p.version,
    )


def _application_out(a: applications_service.ApplicationRecord) -> ApplicationOut:
    return ApplicationOut(
        id=str(a.id),
        member_id=str(a.member_id),
        product_id=str(a.product_id),
        amount=str(a.amount),
        term_months=a.term_months,
        rate_pct=str(a.rate_pct),
        purpose=a.purpose,
        stage=a.stage.value,
        cover_pct=str(a.cover_pct),
        version=a.version,
    )


def _guarantee_out(g: guarantees_service.GuaranteeRecord) -> GuaranteeOut:
    return GuaranteeOut(
        id=str(g.id),
        application_id=str(g.application_id),
        guarantor_member_id=str(g.guarantor_member_id),
        borrower_member_id=str(g.borrower_member_id),
        amount=str(g.amount),
        status=g.status,
        version=g.version,
    )


@router.post("/products", status_code=201)
async def create_product(body: ProductCreateBody, ctx: SettingsCreateCtx) -> ProductOut:
    factory = get_sessionmaker(get_settings().database_url)
    async with tenant_session(factory, ctx.tenant_id) as session:
        product = await products_service.create_product(
            session,
            ctx.tenant_id,
            ctx.user_id,
            name=body.name,
            rate_pct=body.rate_pct,
            deposit_multiplier=body.deposit_multiplier,
            max_term_months=body.max_term_months,
            guarantors_required=body.guarantors_required,
        )
    return _product_out(product)


@router.get("/products")
async def list_products(ctx: ProductListCtx) -> list[ProductOut]:
    factory = get_sessionmaker(get_settings().database_url)
    async with tenant_session(factory, ctx.tenant_id) as session:
        products = await products_service.list_products(session, ctx.tenant_id)
    return [_product_out(p) for p in products]


@router.put("/products/{product_id}")
async def update_product(
    product_id: uuid.UUID,
    body: ProductUpdateBody,
    ctx: SettingsEditCtx,
) -> ProductOut:
    factory = get_sessionmaker(get_settings().database_url)
    async with tenant_session(factory, ctx.tenant_id) as session:
        product = await products_service.update_product(
            session,
            ctx.tenant_id,
            ctx.user_id,
            product_id,
            version=body.version,
            rate_pct=body.rate_pct,
            deposit_multiplier=body.deposit_multiplier,
            max_term_months=body.max_term_months,
            guarantors_required=body.guarantors_required,
            active=body.active,
        )
    return _product_out(product)


@router.post("/applications", status_code=201)
async def create_application(body: ApplicationCreateBody, ctx: AppsCreateCtx) -> ApplicationOut:
    factory = get_sessionmaker(get_settings().database_url)
    async with tenant_session(factory, ctx.tenant_id) as session:
        record = await applications_service.create_application(
            session,
            ctx.tenant_id,
            ctx.user_id,
            member_id=body.member_id,
            product_id=body.product_id,
            amount=body.amount,
            term_months=body.term_months,
            purpose=body.purpose,
        )
    return _application_out(record)


@router.get("/applications")
async def list_applications(
    ctx: AppsViewCtx,
    stage: ApplicationStage | None = None,
    cursor: str | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> ApplicationListResponse:
    factory = get_sessionmaker(get_settings().database_url)
    async with tenant_session(factory, ctx.tenant_id) as session:
        items, next_cursor = await applications_service.list_applications(
            session,
            ctx.tenant_id,
            stage=stage,
            cursor=cursor,
            limit=limit,
        )
    return ApplicationListResponse(
        items=[_application_out(a) for a in items],
        next_cursor=next_cursor,
    )


@router.get("/applications/{application_id}")
async def get_application(application_id: uuid.UUID, ctx: AppsViewCtx) -> ApplicationOut:
    factory = get_sessionmaker(get_settings().database_url)
    async with tenant_session(factory, ctx.tenant_id) as session:
        record = await applications_service.get_application(session, ctx.tenant_id, application_id)
        max_eligible = await applications_service.application_max_eligible(
            session, ctx.tenant_id, record
        )
    out = _application_out(record)
    out.max_eligible = str(max_eligible)
    return out


@router.post("/applications/{application_id}/transition")
async def transition_application(
    application_id: uuid.UUID,
    body: TransitionBody,
    ctx: AppsEditCtx,
) -> ApplicationOut:
    factory = get_sessionmaker(get_settings().database_url)
    async with tenant_session(factory, ctx.tenant_id) as session:
        record = await applications_service.transition_stage(
            session,
            ctx.tenant_id,
            ctx.user_id,
            application_id,
            version=body.version,
            target=body.target,
        )
    return _application_out(record)


@router.post("/applications/{application_id}/vote")
async def vote_on_application(
    application_id: uuid.UUID,
    body: VoteBody,
    ctx: AppsApproveCtx,
) -> VoteResultOut:
    factory = get_sessionmaker(get_settings().database_url)
    async with tenant_session(factory, ctx.tenant_id) as session:
        tally = await applications_service.cast_vote(
            session,
            ctx.tenant_id,
            ctx.user_id,
            application_id,
            body.vote,
        )
    return VoteResultOut(
        approvals=tally.approvals,
        rejections=tally.rejections,
        decision=tally.decision.value if tally.decision else None,
        stage=tally.stage.value,
    )


@router.post("/applications/{application_id}/guarantees", status_code=201)
async def pledge_guarantee(
    application_id: uuid.UUID,
    body: GuaranteePledgeBody,
    ctx: AppsEditCtx,
) -> GuaranteeOut:
    factory = get_sessionmaker(get_settings().database_url)
    async with tenant_session(factory, ctx.tenant_id) as session:
        record = await guarantees_service.pledge_guarantee(
            session,
            ctx.tenant_id,
            ctx.user_id,
            application_id=application_id,
            guarantor_member_id=body.guarantor_member_id,
            amount=body.amount,
        )
    return _guarantee_out(record)


@router.post("/guarantees/{guarantee_id}/consent")
async def consent_guarantee(
    guarantee_id: uuid.UUID,
    body: ConsentBody,
    ctx: AppsEditCtx,
) -> GuaranteeOut:
    factory = get_sessionmaker(get_settings().database_url)
    async with tenant_session(factory, ctx.tenant_id) as session:
        record = await guarantees_service.consent_guarantee(
            session,
            ctx.tenant_id,
            ctx.user_id,
            guarantee_id,
            version=body.version,
        )
    return _guarantee_out(record)
