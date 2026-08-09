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

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field

from genesis.api.authz import RequirePermission, get_auth_context
from genesis.application import guarantees as guarantees_service
from genesis.application import loan_applications as applications_service
from genesis.application import loan_products as products_service
from genesis.application import rbac as rbac_service
from genesis.application.auth import AuthContext
from genesis.domain.committee import Vote
from genesis.domain.lending import ApplicationStage
from genesis.domain.rbac import Action, Module
from genesis.errors import ForbiddenError
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


class RequireProductListPermission(RequirePermission):
    """Allow product discovery to settings viewers and application creators."""

    def __init__(self) -> None:
        super().__init__(Module.SETTINGS, Action.VIEW)

    async def __call__(self, request: Request) -> AuthContext:
        ctx = get_auth_context(request)
        factory = get_sessionmaker(get_settings().database_url)
        async with tenant_session(factory, ctx.tenant_id) as session:
            settings_view = await rbac_service.has_permission(
                session, ctx.role_id, Module.SETTINGS, Action.VIEW
            )
            applications_create = await rbac_service.has_permission(
                session, ctx.role_id, Module.APPLICATIONS, Action.CREATE
            )
        if not (settings_view or applications_create):
            raise ForbiddenError("settings:view or applications:create")
        return ctx

_apps_approve = RequirePermission(Module.APPLICATIONS, Action.APPROVE)
_products_list = RequireProductListPermission()

SettingsViewCtx = Annotated[AuthContext, Depends(_settings_view)]
SettingsCreateCtx = Annotated[AuthContext, Depends(_settings_create)]
SettingsEditCtx = Annotated[AuthContext, Depends(_settings_edit)]
AppsViewCtx = Annotated[AuthContext, Depends(_apps_view)]
AppsCreateCtx = Annotated[AuthContext, Depends(_apps_create)]
AppsEditCtx = Annotated[AuthContext, Depends(_apps_edit)]
AppsApproveCtx = Annotated[AuthContext, Depends(_apps_approve)]
ProductListCtx = Annotated[AuthContext, Depends(_products_list)]


class ProductCreateBody(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    rate_pct: Decimal = Field(gt=0, le=100, max_digits=5, decimal_places=2)
    deposit_multiplier: Decimal = Field(gt=0, max_digits=5, decimal_places=2)
    max_term_months: int = Field(ge=1, le=120)


class ProductUpdateBody(BaseModel):
    version: int = Field(ge=1)
    rate_pct: Decimal | None = Field(default=None, gt=0, le=100, max_digits=5, decimal_places=2)
    deposit_multiplier: Decimal | None = Field(default=None, gt=0, max_digits=5, decimal_places=2)
    max_term_months: int | None = Field(default=None, ge=1, le=120)
    active: bool | None = None


class ProductOut(BaseModel):
    id: str
    name: str
    rate_pct: str
    deposit_multiplier: str
    max_term_months: int
    active: bool
    version: int


class ApplicationCreateBody(BaseModel):
    member_id: uuid.UUID
    product_id: uuid.UUID
    amount: Decimal = Field(gt=0, le=1_000_000_000)
    term_months: int = Field(ge=1)
    purpose: str | None = Field(default=None, max_length=500)


class ApplicationOut(BaseModel):
    id: str
    member_id: str
    member_no: str
    member_name: str
    product_id: str
    amount: str
    term_months: int
    rate_pct: str
    purpose: str | None
    stage: str
    cover_pct: str
    version: int


class ApplicationListResponse(BaseModel):
    items: list[ApplicationOut]
    next_cursor: str | None


class TransitionBody(BaseModel):
    version: int = Field(ge=1)
    target: ApplicationStage


class VoteBody(BaseModel):
    vote: Vote


class VoteResultOut(BaseModel):
    approvals: int
    rejections: int
    decision: str | None
    stage: str


class GuaranteePledgeBody(BaseModel):
    guarantor_member_id: uuid.UUID
    amount: Decimal = Field(gt=0, le=1_000_000_000)


class ConsentBody(BaseModel):
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
        active=p.active,
        version=p.version,
    )


def _application_out(a: applications_service.ApplicationRecord) -> ApplicationOut:
    return ApplicationOut(
        id=str(a.id),
        member_id=str(a.member_id),
        member_no=a.member_no,
        member_name=a.member_name,
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
        )
    return _product_out(product)


@router.get("/products")
async def list_products(ctx: ProductListCtx) -> list[ProductOut]:
    factory = get_sessionmaker(get_settings().database_url)
    async with tenant_session(factory, ctx.tenant_id) as session:
        products = await products_service.list_products(session)
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
        record = await applications_service.get_application(session, application_id)
    return _application_out(record)


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
