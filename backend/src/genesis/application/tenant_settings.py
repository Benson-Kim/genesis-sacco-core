"""Tenant settings management + consumer read path (P13.7, gates 1.1-1.6).

Generalises the 0009/0010 tenant_settings row into the prototype
Settings screens' backend. Settings ARE the money parameters (rates,
fees, quorums, authority ceilings), so this module is the SINGLE
legitimate writer (gate 1.6 v1.1 rule 1): the API layer rejects
unknown fields (extra="forbid" -> 422) and this service re-checks every
key against the code-owned registry (genesis/domain/tenant_config.py)
— a caller can never invent a setting, and every accepted key has a
declared type, unit, bounds (DB CHECK, migration 0017) and consumer
list.

Concurrency (gate 1.4): one settings row per tenant (PK tenant_id).
Writes are optimistic-locked single-row, single-statement transactions:
  * first-time configuration claims the row atomically with
    INSERT ... ON CONFLICT DO NOTHING checked by rowcount (v1.1 rule 5;
    version 0 means "no row yet"),
  * subsequent edits UPDATE ... WHERE version = :ver and surface 409 on
    a stale version (exactly one of two concurrent writers wins).
No SELECT ... FOR UPDATE is taken anywhere in this module, so no lock-
ordering cycle with the P12 settlement chain (member -> accounts ->
loans) or the P13.5 admin-set chain is possible: consumers read the
settings row with plain MVCC snapshot reads while already holding
their own domain locks.

Band configuration is validated at WRITE and REVALIDATED at READ
before any consumer acts on it (defence against manual SQL edits — the
0017 DB CHECK can only pin the JSON top-level type). Corrupted stored
bands fail CLOSED for CONSUMERS: the money path gets a 409 conflict,
never a silently skipped guard. The settings VIEWER (GET /settings)
instead degrades per-key (review R3): a corrupt key is returned as
null and surfaced by NAME ONLY in ``corrupt_keys`` (never the corrupt
payload — least disclosure), keeping ``version`` readable so an
operator can repair the key via PUT instead of guessing versions
blind.

Every read AND write carries an explicit bound tenant_id predicate on
top of forced RLS (v1.1 rule 4); every mutation writes its audit row
in-transaction with the exact before/after figures (gate 1.5; least-
disclosure errors carry only a category to the caller, gate 1.6).
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Any, cast

from sqlalchemy import CursorResult, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from genesis.application.audit import record_audit
from genesis.domain.committee import COMMITTEE_QUORUM
from genesis.domain.tenant_config import (
    SETTINGS_REGISTRY,
    ApprovalBand,
    BandConfigError,
    LoanInterestBasis,
    LoanInterestMethod,
    PenaltyChargedOn,
    RateBand,
    authority_may_ratify,
    validate_approval_bands,
    validate_loan_rate_bands,
)
from genesis.errors import ConflictError, ForbiddenError, InvalidInputError

#: Code-owned column order for the settings row (v1.1 rule 6: these
#: identifiers come from the registry, never from caller input).
_SETTING_KEYS: tuple[str, ...] = tuple(SETTINGS_REGISTRY)

_ENUM_TYPES: dict[str, type[StrEnum]] = {
    "penalty_charged_on": PenaltyChargedOn,
    "loan_interest_method": LoanInterestMethod,
    "loan_interest_basis": LoanInterestBasis,
}

_BAND_VALIDATORS: dict[str, Callable[[object], tuple[Any, ...]]] = {
    "loan_rate_bands": validate_loan_rate_bands,
    "approval_bands": validate_approval_bands,
}


@dataclass(frozen=True)
class TenantSettingsRecord:
    """The full settings row (or the all-unconfigured default view).

    ``version`` 0 means the tenant has no settings row yet; a first
    write must claim it with version=0. ``None`` values mean "not
    configured": consumers fall back to their code-owned defaults.
    ``corrupt_keys`` names (by key only, review R3) stored band values
    that failed read-side revalidation and were degraded to None in
    this VIEW — consumers of those keys keep failing closed.
    """

    configured: bool
    version: int
    deposit_interest_annual_rate_pct: Decimal | None
    dividend_rate_pct: Decimal | None
    deposit_rebate_rate_pct: Decimal | None
    penalty_rate_pct_per_month: Decimal | None
    penalty_grace_days: int | None
    penalty_charged_on: PenaltyChargedOn | None
    loan_interest_method: LoanInterestMethod | None
    loan_interest_basis: LoanInterestBasis | None
    loan_rate_bands: tuple[RateBand, ...] | None
    min_share_capital: Decimal | None
    registration_fee: Decimal | None
    min_monthly_contribution: Decimal | None
    max_member_exposure: Decimal | None
    dormancy_period_months: int | None
    financial_year_end_month: int | None
    exit_notice_period_days: int | None
    exit_fee: Decimal | None
    committee_size: int | None
    committee_quorum: int | None
    approval_bands: tuple[ApprovalBand, ...] | None
    corrupt_keys: tuple[str, ...] = ()


def settings_row_sql() -> str:
    """The settings-row lookup (single PK probe, gate 1.3).

    Column identifiers come from the code-owned registry, never caller
    input (v1.1 rule 6); the tenant predicate is explicit on top of
    forced RLS (v1.1 rule 4). Exported so the EXPLAIN capture
    (tests/test_p137_explain.py) asserts against the production SQL.
    """
    columns = ", ".join(_SETTING_KEYS)
    return (
        f"SELECT version, {columns} FROM tenant_settings "  # noqa: S608
        "WHERE tenant_id = CAST(:tid AS uuid)"
    )


#: Consumer reads (P9/P12 quorum, P9 authority bands): single-column PK
#: probes, exported for the EXPLAIN capture. Explicit tenant predicate
#: on top of forced RLS (v1.1 rule 4).
COMMITTEE_QUORUM_SQL = (
    "SELECT committee_quorum FROM tenant_settings WHERE tenant_id = CAST(:tid AS uuid)"
)
APPROVAL_BANDS_SQL = (
    "SELECT approval_bands FROM tenant_settings WHERE tenant_id = CAST(:tid AS uuid)"
)


async def _fetch_raw(session: AsyncSession, tenant_id: uuid.UUID) -> dict[str, object] | None:
    """The settings row as {key: raw value}, or None. Single PK lookup."""
    row = (await session.execute(text(settings_row_sql()), {"tid": str(tenant_id)})).first()
    if row is None:
        return None
    raw: dict[str, object] = {"version": int(row[0])}
    for index, key in enumerate(_SETTING_KEYS, start=1):
        raw[key] = row[index]
    return raw


def _revalidated_bands(key: str, value: object) -> tuple[Any, ...] | None:
    """Read-side band revalidation; corrupted stored config fails closed."""
    if value is None:
        return None
    try:
        return _BAND_VALIDATORS[key](value)
    except BandConfigError as exc:
        # Least disclosure: the caller sees a 409 category; the exact
        # defect stays in this internal message (gate 1.6).
        raise ConflictError(f"stored {key} failed revalidation: {exc}") from exc


def _record_from_raw(raw: dict[str, object] | None) -> TenantSettingsRecord:
    def dec(key: str) -> Decimal | None:
        value = raw[key] if raw else None
        return Decimal(str(value)) if value is not None else None

    def num(key: str) -> int | None:
        value = raw[key] if raw else None
        return int(cast(int, value)) if value is not None else None

    def enum(key: str) -> Any:
        value = raw[key] if raw else None
        return _ENUM_TYPES[key](str(value)) if value is not None else None

    corrupt: list[str] = []

    def bands(key: str) -> tuple[Any, ...] | None:
        value = raw[key] if raw else None
        if value is None:
            return None
        try:
            return _BAND_VALIDATORS[key](value)
        except BandConfigError:
            # Viewer-side degradation (review R3): the settings VIEW
            # names the corrupt key (never its payload) so an operator
            # can repair via PUT with the version this read returns.
            # Consumers keep failing CLOSED via _revalidated_bands.
            corrupt.append(key)
            return None

    rate_bands = cast("tuple[RateBand, ...] | None", bands("loan_rate_bands"))
    approval = cast("tuple[ApprovalBand, ...] | None", bands("approval_bands"))
    return TenantSettingsRecord(
        configured=raw is not None,
        version=int(cast(int, raw["version"])) if raw else 0,
        deposit_interest_annual_rate_pct=dec("deposit_interest_annual_rate_pct"),
        dividend_rate_pct=dec("dividend_rate_pct"),
        deposit_rebate_rate_pct=dec("deposit_rebate_rate_pct"),
        penalty_rate_pct_per_month=dec("penalty_rate_pct_per_month"),
        penalty_grace_days=num("penalty_grace_days"),
        penalty_charged_on=enum("penalty_charged_on"),
        loan_interest_method=enum("loan_interest_method"),
        loan_interest_basis=enum("loan_interest_basis"),
        loan_rate_bands=rate_bands,
        min_share_capital=dec("min_share_capital"),
        registration_fee=dec("registration_fee"),
        min_monthly_contribution=dec("min_monthly_contribution"),
        max_member_exposure=dec("max_member_exposure"),
        dormancy_period_months=num("dormancy_period_months"),
        financial_year_end_month=num("financial_year_end_month"),
        exit_notice_period_days=num("exit_notice_period_days"),
        exit_fee=dec("exit_fee"),
        committee_size=num("committee_size"),
        committee_quorum=num("committee_quorum"),
        approval_bands=approval,
        corrupt_keys=tuple(corrupt),
    )


async def get_settings(session: AsyncSession, tenant_id: uuid.UUID) -> TenantSettingsRecord:
    """The tenant's settings (single PK lookup); defaults when no row.

    Stored band JSON is revalidated here too. A corrupted value never
    flows onward: it is degraded to None in this VIEW and named in
    corrupt_keys (review R3) so the operator keeps a readable version
    for the repair PUT, while every consumer of the key keeps failing
    closed (409) via _revalidated_bands.
    """
    return _record_from_raw(await _fetch_raw(session, tenant_id))


def _normalise(key: str, value: object) -> tuple[object, object]:
    """(bound SQL parameter, audit payload value) for one registry key.

    The key is guaranteed to be in SETTINGS_REGISTRY by the caller.
    Band lists are validated with the domain contract at WRITE; enums
    are pinned to their code-owned members; a None clears the setting
    (consumers fall back to their defaults).
    """
    if value is None:
        return None, None
    spec = SETTINGS_REGISTRY[key]
    if spec.value_type.startswith("bands:"):
        try:
            _BAND_VALIDATORS[key](value)
        except BandConfigError as exc:
            raise InvalidInputError(f"invalid {key}: {exc}") from exc
        return json.dumps(value), value
    if spec.value_type.startswith("enum:"):
        member = _ENUM_TYPES[key](value.value if isinstance(value, StrEnum) else str(value))
        return member.value, member.value
    if spec.value_type == "integer":
        return int(cast(int, value)), int(cast(int, value))
    # money / percent: NUMERIC columns take the exact decimal string.
    return str(Decimal(str(value))), str(Decimal(str(value)))


def _check_quorum_size(quorum: int | None, size: int | None) -> None:
    """Cross-field bound, mirrored by ck_tenant_settings_quorum_le_size."""
    if quorum is not None and size is not None and quorum > size:
        raise InvalidInputError("committee_quorum must not exceed committee_size")


async def update_settings(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID,
    *,
    version: int,
    changes: dict[str, object],
) -> TenantSettingsRecord:
    """Optimistic-locked partial update of the settings row (gate 1.4).

    ``changes`` maps registry keys to new values (None clears a key).
    version=0 claims a not-yet-existing row atomically (v1.1 rule 5);
    version>=1 edits the existing row, 409 on stale. Every mutation
    writes an audit row with the exact before/after figures (gate 1.5).
    """
    unknown = sorted(set(changes) - set(_SETTING_KEYS))
    if unknown:
        # Second fence behind the API's extra="forbid": the service
        # itself refuses keys outside the code-owned registry.
        raise InvalidInputError(f"unknown setting keys: {unknown}")
    if not changes:
        raise InvalidInputError("no settings provided")
    current = await _fetch_raw(session, tenant_id)
    params: dict[str, object] = {"tid": str(tenant_id)}
    audit_after: dict[str, object] = {}
    for key, value in changes.items():
        params[key], audit_after[key] = _normalise(key, value)
    merged_quorum = cast(
        "int | None",
        params.get("committee_quorum", current["committee_quorum"] if current else None),
    )
    merged_size = cast(
        "int | None",
        params.get("committee_size", current["committee_size"] if current else None),
    )
    _check_quorum_size(merged_quorum, merged_size)
    changed_keys = sorted(changes)  # code-owned registry keys (v1.1 rule 6)

    if version == 0:
        if current is not None:
            raise ConflictError(f"settings already configured for tenant {tenant_id}")
        columns = ", ".join(changed_keys)
        placeholders = ", ".join(
            f"CAST(:{k} AS jsonb)" if k in _BAND_VALIDATORS else f":{k}" for k in changed_keys
        )
        try:
            claimed = (
                await session.execute(
                    text(
                        "INSERT INTO tenant_settings "  # noqa: S608
                        f"(tenant_id, {columns}) "
                        f"VALUES (CAST(:tid AS uuid), {placeholders}) "
                        "ON CONFLICT (tenant_id) DO NOTHING RETURNING version"
                    ),
                    params,
                )
            ).first()
        except IntegrityError as exc:
            # A DB CHECK refused a bound the API validation also pins;
            # surfaced as a validation category (gate 1.6).
            raise InvalidInputError("settings value outside its permitted bounds") from exc
        if claimed is None:
            raise ConflictError(f"settings were configured concurrently for tenant {tenant_id}")
        before: dict[str, object] | None = None
        new_version = 1
    else:
        if current is None:
            raise ConflictError(f"stale version {version}: settings not configured yet")
        assignments = ", ".join(
            f"{k} = CAST(:{k} AS jsonb)" if k in _BAND_VALIDATORS else f"{k} = :{k}"
            for k in changed_keys
        )
        params["ver"] = version
        try:
            result = cast(
                CursorResult[Any],
                await session.execute(
                    text(
                        "UPDATE tenant_settings "  # noqa: S608
                        f"SET {assignments}, version = version + 1, updated_at = now() "
                        "WHERE tenant_id = CAST(:tid AS uuid) AND version = :ver"
                    ),
                    params,
                ),
            )
        except IntegrityError as exc:
            raise InvalidInputError("settings value outside its permitted bounds") from exc
        if result.rowcount != 1:
            raise ConflictError(f"stale version {version} for tenant settings")
        before = {key: _json_safe(current[key]) for key in changed_keys}
        before["version"] = version
        new_version = version + 1
    audit_after["version"] = new_version
    await record_audit(
        session,
        tenant_id,
        actor_id,
        action="tenant_settings.update",
        entity="tenant_settings",
        entity_id=str(tenant_id),
        before=before,
        after=audit_after,
    )
    return _record_from_raw(await _fetch_raw(session, tenant_id))


def _json_safe(value: object) -> object:
    """Audit payloads are JSONB: Decimals become their exact strings."""
    return str(value) if isinstance(value, Decimal) else value


async def committee_quorum(session: AsyncSession, tenant_id: uuid.UUID) -> int:
    """The quorum consumers decide votes with, read AT VOTE TIME.

    Falls back to the code-owned COMMITTEE_QUORUM constant when the
    tenant has no settings row or has not configured a quorum
    (BUILD_PROMPTS P13.7: current constants as fallback defaults).
    Callers invoke this inside their own transaction while holding the
    application/exit row lock, so an in-flight vote binds to the config
    committed at its own snapshot point (v1.1 rule 3) — a later config
    change applies to later votes only, never retroactively.
    """
    row = (await session.execute(text(COMMITTEE_QUORUM_SQL), {"tid": str(tenant_id)})).first()
    if row is None or row[0] is None:
        return COMMITTEE_QUORUM
    return int(row[0])


async def approval_bands_config(
    session: AsyncSession, tenant_id: uuid.UUID
) -> tuple[ApprovalBand, ...] | None:
    """The approval matrix, revalidated at READ; None when unconfigured.

    Corrupted stored bands (possible only via manual SQL, never via the
    API) fail CLOSED with a 409 instead of silently disabling the
    authority guard.
    """
    row = (await session.execute(text(APPROVAL_BANDS_SQL), {"tid": str(tenant_id)})).first()
    if row is None or row[0] is None:
        return None
    return cast("tuple[ApprovalBand, ...]", _revalidated_bands("approval_bands", row[0]))


async def enforce_authority_band(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID,
    amount: Decimal,
) -> None:
    """Refuse a ratifying action above the actor's authority band (403).

    Callers MUST already hold the application row lock: the matrix is
    then read from the current committed config at the transition's own
    snapshot point, so a config change mid-workflow governs future
    transitions only, never retroactively (v1.1 rule 3). The actor's
    role is resolved server-side from the users table inside this
    transaction — never from the JWT alone (the P13.5 F1/F2 precedent).
    A configured matrix caps EVERY role: roles not listed fail closed
    at the FIRST band's ceiling (review R2 — misconfiguration degrades
    safely, never uncaps); the P4 RBAC matrix remains the primary
    gate. Only a tenant with NO matrix configured keeps the pre-P13.7
    uncapped behaviour (fallback default).
    """
    bands = await approval_bands_config(session, tenant_id)
    if bands is None:
        return
    row = (
        await session.execute(
            text(
                "SELECT r.name FROM users u "
                "JOIN roles r ON r.id = u.role_id AND r.tenant_id = CAST(:tid AS uuid) "
                "WHERE u.id = CAST(:uid AS uuid) AND u.tenant_id = CAST(:tid AS uuid)"
            ),
            {"uid": str(actor_id), "tid": str(tenant_id)},
        )
    ).first()
    if row is None:
        # Fail closed: an actor the users table cannot vouch for has no
        # ratification authority (least disclosure, gate 1.6).
        raise ForbiddenError(f"actor {actor_id} has no resolvable role for authority check")
    role_name = str(row[0])
    if not authority_may_ratify(role_name, amount, bands):
        # Least disclosure: no figures echoed; the audit rows of the
        # surrounding workflow carry the exact amounts (gate 1.6).
        raise ForbiddenError(
            f"role '{role_name}' may not ratify this amount under the approval matrix"
        )
