"""Tenant settings endpoints (least disclosure).

The prototype Settings screens' backend (Interest / Parameters /
Approval matrix tabs). Settings ARE the money parameters, so this API
is their single legitimate writer: the request body
rejects unknown fields (extra="forbid" -> 422, so an unknown setting
key can never ride in), every known field carries the same bounds the
DB CHECKs enforce (migration 0017), and band payloads are validated
against the code-owned domain contract at the boundary. Routes carry
RequirePermission settings x view/edit (deny by default); mutations
are idempotent via the Idempotency-Key middleware and optimistic-
locked via the version field (concurrency safety).

Money amounts travel as decimal strings (the codebase-wide contract):
a JSON float in a band payload is rejected, never rounded.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field, field_validator

from genesis.api.authz import RequirePermission
from genesis.application import tenant_settings as settings_service
from genesis.application.auth import AuthContext
from genesis.domain.rbac import Action, Module
from genesis.domain.tenant_config import (
    ApprovalBand,
    BandConfigError,
    LoanInterestBasis,
    LoanInterestMethod,
    PenaltyChargedOn,
    RateBand,
    validate_approval_bands,
    validate_loan_rate_bands,
)
from genesis.infrastructure.db import get_sessionmaker
from genesis.infrastructure.tenancy import tenant_session
from genesis.settings import get_settings as get_env_settings

router = APIRouter(prefix="/settings", tags=["settings"])

_view = RequirePermission(Module.SETTINGS, Action.VIEW)
_edit = RequirePermission(Module.SETTINGS, Action.EDIT)

ViewCtx = Annotated[AuthContext, Depends(_view)]
EditCtx = Annotated[AuthContext, Depends(_edit)]


class SettingsUpdateBody(BaseModel):
    """Partial update: absent keys keep their values; null clears a key.

    Field bounds mirror the 0017 DB CHECKs 1:1 (the registry in
    genesis/domain/tenant_config.py documents each pair); the drift
    test pins this model's fields to the registry keys.
    """

    model_config = ConfigDict(extra="forbid")

    version: int = Field(ge=0)
    deposit_interest_annual_rate_pct: Decimal | None = Field(
        default=None, gt=0, le=100, max_digits=5, decimal_places=2
    )
    dividend_rate_pct: Decimal | None = Field(
        default=None, ge=0, le=100, max_digits=5, decimal_places=2
    )
    deposit_rebate_rate_pct: Decimal | None = Field(
        default=None, ge=0, le=100, max_digits=5, decimal_places=2
    )
    penalty_rate_pct_per_month: Decimal | None = Field(
        default=None, ge=0, le=100, max_digits=5, decimal_places=2
    )
    penalty_grace_days: int | None = Field(default=None, ge=0, le=365)
    penalty_charged_on: PenaltyChargedOn | None = None
    loan_interest_method: LoanInterestMethod | None = None
    loan_interest_basis: LoanInterestBasis | None = None
    loan_rate_bands: list[dict[str, object]] | None = None
    min_share_capital: Decimal | None = Field(default=None, ge=0, max_digits=18, decimal_places=2)
    registration_fee: Decimal | None = Field(default=None, ge=0, max_digits=18, decimal_places=2)
    min_monthly_contribution: Decimal | None = Field(
        default=None, ge=0, max_digits=18, decimal_places=2
    )
    max_member_exposure: Decimal | None = Field(default=None, ge=0, max_digits=18, decimal_places=2)
    dormancy_period_months: int | None = Field(default=None, ge=1, le=120)
    financial_year_end_month: int | None = Field(default=None, ge=1, le=12)
    exit_notice_period_days: int | None = Field(default=None, ge=0, le=365)
    exit_fee: Decimal | None = Field(default=None, ge=0, max_digits=18, decimal_places=2)
    committee_size: int | None = Field(default=None, ge=1, le=25)
    committee_quorum: int | None = Field(default=None, ge=1, le=25)
    approval_bands: list[dict[str, object]] | None = None

    @field_validator("loan_rate_bands")
    @classmethod
    def _rate_bands_contract(
        cls, value: list[dict[str, object]] | None
    ) -> list[dict[str, object]] | None:
        """Boundary validation (422); the service revalidates at write."""
        if value is not None:
            try:
                validate_loan_rate_bands(value)
            except BandConfigError as exc:
                raise ValueError(str(exc)) from exc
        return value

    @field_validator("approval_bands")
    @classmethod
    def _approval_bands_contract(
        cls, value: list[dict[str, object]] | None
    ) -> list[dict[str, object]] | None:
        """Boundary validation (422); the service revalidates at write."""
        if value is not None:
            try:
                validate_approval_bands(value)
            except BandConfigError as exc:
                raise ValueError(str(exc)) from exc
        return value


class SettingsOut(BaseModel):
    """The full settings view; null means "not configured" (fallbacks
    documented per key in the registry). Decimals travel as strings.
    ``corrupt_keys`` names stored band keys that failed read-side
    revalidation (degraded to null in this view) — names
    only, never the corrupt payload (least disclosure)."""

    configured: bool
    version: int
    corrupt_keys: list[str]
    deposit_interest_annual_rate_pct: str | None
    dividend_rate_pct: str | None
    deposit_rebate_rate_pct: str | None
    penalty_rate_pct_per_month: str | None
    penalty_grace_days: int | None
    penalty_charged_on: str | None
    loan_interest_method: str | None
    loan_interest_basis: str | None
    loan_rate_bands: list[dict[str, str | None]] | None
    min_share_capital: str | None
    registration_fee: str | None
    min_monthly_contribution: str | None
    max_member_exposure: str | None
    dormancy_period_months: int | None
    financial_year_end_month: int | None
    exit_notice_period_days: int | None
    exit_fee: str | None
    committee_size: int | None
    committee_quorum: int | None
    approval_bands: list[dict[str, str | None]] | None


def _rate_bands_out(bands: tuple[RateBand, ...] | None) -> list[dict[str, str | None]] | None:
    if bands is None:
        return None
    return [
        {
            "min_amount": str(b.min_amount),
            "max_amount": str(b.max_amount) if b.max_amount is not None else None,
            "rate_pct": str(b.rate_pct),
        }
        for b in bands
    ]


def _approval_bands_out(
    bands: tuple[ApprovalBand, ...] | None,
) -> list[dict[str, str | None]] | None:
    if bands is None:
        return None
    return [
        {
            "authority": b.authority,
            "max_amount": str(b.max_amount) if b.max_amount is not None else None,
        }
        for b in bands
    ]


def _out(record: settings_service.TenantSettingsRecord) -> SettingsOut:
    def s(value: Decimal | None) -> str | None:
        return str(value) if value is not None else None

    return SettingsOut(
        configured=record.configured,
        version=record.version,
        corrupt_keys=list(record.corrupt_keys),
        deposit_interest_annual_rate_pct=s(record.deposit_interest_annual_rate_pct),
        dividend_rate_pct=s(record.dividend_rate_pct),
        deposit_rebate_rate_pct=s(record.deposit_rebate_rate_pct),
        penalty_rate_pct_per_month=s(record.penalty_rate_pct_per_month),
        penalty_grace_days=record.penalty_grace_days,
        penalty_charged_on=(
            record.penalty_charged_on.value if record.penalty_charged_on is not None else None
        ),
        loan_interest_method=(
            record.loan_interest_method.value if record.loan_interest_method is not None else None
        ),
        loan_interest_basis=(
            record.loan_interest_basis.value if record.loan_interest_basis is not None else None
        ),
        loan_rate_bands=_rate_bands_out(record.loan_rate_bands),
        min_share_capital=s(record.min_share_capital),
        registration_fee=s(record.registration_fee),
        min_monthly_contribution=s(record.min_monthly_contribution),
        max_member_exposure=s(record.max_member_exposure),
        dormancy_period_months=record.dormancy_period_months,
        financial_year_end_month=record.financial_year_end_month,
        exit_notice_period_days=record.exit_notice_period_days,
        exit_fee=s(record.exit_fee),
        committee_size=record.committee_size,
        committee_quorum=record.committee_quorum,
        approval_bands=_approval_bands_out(record.approval_bands),
    )


@router.get("")
async def get_settings(ctx: ViewCtx) -> SettingsOut:
    factory = get_sessionmaker(get_env_settings().database_url)
    async with tenant_session(factory, ctx.tenant_id) as session:
        record = await settings_service.get_settings(session, ctx.tenant_id)
    return _out(record)


@router.put("")
async def update_settings(body: SettingsUpdateBody, ctx: EditCtx) -> SettingsOut:
    """Optimistic-locked settings write; version 0 claims the first row."""
    changes = body.model_dump(exclude_unset=True)
    changes.pop("version", None)
    factory = get_sessionmaker(get_env_settings().database_url)
    async with tenant_session(factory, ctx.tenant_id) as session:
        record = await settings_service.update_settings(
            session,
            ctx.tenant_id,
            ctx.user_id,
            version=body.version,
            changes=changes,
        )
    return _out(record)
