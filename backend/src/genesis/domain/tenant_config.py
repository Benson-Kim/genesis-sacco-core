"""Tenant configuration registry and band rules (P13.7). Pure logic.

Single source of truth for every tenant-configurable money parameter
(gate 1.6 v1.1 rule 1: the settings API is the single legitimate
writer, and these code-owned keys are the only keys it accepts —
callers can never invent a setting).

Three pieces live here, all free of I/O (MASTER_PROMPT 2.1):

  * SETTINGS_REGISTRY — the typed setting registry. Every key declares
    its group, type, unit, bounds (mirrored 1:1 by a DB CHECK shipped
    in migration 0017) and consumer list, so drift between the API
    surface, the schema and the consumers is caught by static tests
    (tests/test_tenant_config_domain.py), not discovered in production.
  * Tiered loan-rate band validation — enforced at WRITE by the API
    and service, and REVALIDATED at READ before any consumer uses the
    stored JSON (defence against manual SQL edits: the DB CHECK can
    only pin the top-level type).
  * Approval-matrix band validation + deterministic band resolution —
    bands are contiguous, strictly increasing amount ranges, so every
    amount resolves to exactly one band (or, above a finite top band,
    to "no listed authority", the explicit top-band rule).

Band containment convention (documented, used by both band kinds): a
band with bounds (lower, upper] covers amounts strictly greater than
its lower bound and up to & including its upper bound; the first band's
lower bound is zero; a NULL upper bound means "unbounded" and is only
legal on the last band. Lookups are linear scans over lists whose size
is bounded in code (MAX_RATE_BANDS / len(ROLE_NAMES)) — O(n) with a
documented tiny n, cheaper in practice than maintaining a parallel
bisect structure (docs/DSA_HARDENING.md evidence style).
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from genesis.domain.rbac import ROLE_NAMES

#: NUMERIC(18,2) ceiling — amounts at or above this overflow the column.
MAX_MONEY = Decimal("9999999999999999.99")

#: Percentage columns are NUMERIC(5,2) with CHECK <= 100.
MAX_PERCENT = Decimal("100")

#: Upper bound on stored rate bands: enough for any tiered price sheet,
#: small enough that the linear containment scan is trivially O(1).
MAX_RATE_BANDS = 20


class PenaltyChargedOn(enum.StrEnum):
    """Basis the arrears penalty is charged on (prototype Interest tab)."""

    INSTALMENT_IN_ARREARS = "instalment_in_arrears"
    FULL_OUTSTANDING = "full_outstanding"


class LoanInterestMethod(enum.StrEnum):
    """Stored only: the P6 engine extension is out of scope (P13.7)."""

    REDUCING_BALANCE = "reducing_balance"
    FLAT = "flat"


class LoanInterestBasis(enum.StrEnum):
    """Stored only: the P6 engine extension is out of scope (P13.7)."""

    THIRTY_360 = "thirty_360"
    ACTUAL_365 = "actual_365"


class SettingGroup(enum.StrEnum):
    INTEREST = "interest"
    PARAMETERS = "parameters"
    APPROVAL = "approval"


@dataclass(frozen=True)
class SettingSpec:
    """Declaration of one tenant-configurable key (code-owned).

    ``bounds`` documents the closed contract that the DB CHECK in
    migration 0017 (or 0009/0010 for the two legacy keys) enforces;
    ``consumers`` names every code path that reads the key, so a new
    consumer (or an orphaned key) is a reviewable diff here.
    """

    key: str
    group: SettingGroup
    value_type: str
    unit: str
    bounds: str
    consumers: tuple[str, ...]


def _spec(
    key: str,
    group: SettingGroup,
    value_type: str,
    unit: str,
    bounds: str,
    consumers: tuple[str, ...],
) -> tuple[str, SettingSpec]:
    return key, SettingSpec(key, group, value_type, unit, bounds, consumers)


#: The typed setting registry (P13.7). Keys are the tenant_settings
#: column names; the settings API accepts exactly these keys (unknown
#: keys are a 422 via extra="forbid") and the completeness tests pin
#: API model <-> registry <-> DB columns to each other.
SETTINGS_REGISTRY: dict[str, SettingSpec] = dict(
    (
        _spec(
            "deposit_interest_annual_rate_pct",
            SettingGroup.INTEREST,
            "percent",
            "% per annum",
            "> 0 and <= 100 (0009 CHECK)",
            ("application/deposit_interest.resolve_run_parameters",),
        ),
        _spec(
            "dividend_rate_pct",
            SettingGroup.INTEREST,
            "percent",
            "% per annum",
            ">= 0 and <= 100",
            ("application/dividends.resolve_dividend_config",),
        ),
        _spec(
            "deposit_rebate_rate_pct",
            SettingGroup.INTEREST,
            "percent",
            "% per annum",
            ">= 0 and <= 100 (0020 CHECK)",
            ("application/dividends.resolve_dividend_config",),
        ),
        _spec(
            "penalty_rate_pct_per_month",
            SettingGroup.INTEREST,
            "percent",
            "% per month",
            ">= 0 and <= 100",
            ("application/arrears.resolve_penalty_config",),
        ),
        _spec(
            "penalty_grace_days",
            SettingGroup.INTEREST,
            "integer",
            "days",
            ">= 0 and <= 365",
            ("application/arrears.resolve_penalty_config",),
        ),
        _spec(
            "penalty_charged_on",
            SettingGroup.INTEREST,
            "enum:PenaltyChargedOn",
            "basis",
            "instalment_in_arrears | full_outstanding",
            ("application/arrears.resolve_penalty_config",),
        ),
        _spec(
            "loan_interest_method",
            SettingGroup.INTEREST,
            "enum:LoanInterestMethod",
            "method",
            "reducing_balance | flat",
            ("stored only: P6 engine extension out of scope (P13.7)",),
        ),
        _spec(
            "loan_interest_basis",
            SettingGroup.INTEREST,
            "enum:LoanInterestBasis",
            "day-count basis",
            "thirty_360 | actual_365",
            ("stored only: P6 engine extension out of scope (P13.7)",),
        ),
        _spec(
            "loan_rate_bands",
            SettingGroup.INTEREST,
            "bands:rate",
            "KES bands -> % per annum",
            "validate_loan_rate_bands (write AND read)",
            ("stored for tiered pricing; revalidated readers only",),
        ),
        _spec(
            "min_share_capital",
            SettingGroup.PARAMETERS,
            "money",
            "KES",
            ">= 0",
            ("stored for P13.12 registration flow",),
        ),
        _spec(
            "registration_fee",
            SettingGroup.PARAMETERS,
            "money",
            "KES",
            ">= 0",
            ("stored for P13.12 registration flow",),
        ),
        _spec(
            "min_monthly_contribution",
            SettingGroup.PARAMETERS,
            "money",
            "KES",
            ">= 0",
            ("stored for P13.12 contribution plan",),
        ),
        _spec(
            "max_member_exposure",
            SettingGroup.PARAMETERS,
            "money",
            "KES",
            ">= 0",
            ("stored: exposure rule decision tracked in GAP_ANALYSIS 2.4",),
        ),
        _spec(
            "dormancy_period_months",
            SettingGroup.PARAMETERS,
            "integer",
            "months",
            ">= 1 and <= 120",
            ("application/dormancy.resolve_dormancy_period",),
        ),
        _spec(
            "financial_year_end_month",
            SettingGroup.PARAMETERS,
            "integer",
            "calendar month",
            ">= 1 and <= 12",
            ("application/dividends.resolve_dividend_config",),
        ),
        _spec(
            "exit_notice_period_days",
            SettingGroup.PARAMETERS,
            "integer",
            "days",
            ">= 0 and <= 365",
            ("stored: enforcement recorded as a follow-up in the P13.7 MR",),
        ),
        _spec(
            "exit_fee",
            SettingGroup.PARAMETERS,
            "money",
            "KES",
            ">= 0 (0010 CHECK)",
            ("application/member_exits._exit_fee",),
        ),
        _spec(
            "committee_size",
            SettingGroup.APPROVAL,
            "integer",
            "members",
            ">= 1 and <= 25",
            ("governance metadata; quorum CHECK ties to it",),
        ),
        _spec(
            "committee_quorum",
            SettingGroup.APPROVAL,
            "integer",
            "votes on one side",
            ">= 1 and <= 25 and <= committee_size",
            (
                "application/loan_applications.cast_vote",
                "application/member_exits.cast_exit_vote",
            ),
        ),
        _spec(
            "approval_bands",
            SettingGroup.APPROVAL,
            "bands:approval",
            "KES bands -> authority",
            "validate_approval_bands (write AND read)",
            (
                "application/loan_applications.transition_stage",
                "application/loan_applications.cast_vote",
            ),
        ),
    )
)


class BandConfigError(ValueError):
    """A band list fails the write/read validation contract."""


@dataclass(frozen=True)
class RateBand:
    """One tiered-rate band: amount in (min_amount, max_amount] -> rate."""

    min_amount: Decimal
    max_amount: Decimal | None
    rate_pct: Decimal


@dataclass(frozen=True)
class ApprovalBand:
    """One approval-authority band, cumulative-ceiling semantics.

    The band at index i covers amounts in (previous max, max_amount];
    an authority listed at index i may ratify any amount whose band
    index is <= i (its ceiling is its own max_amount). Authorities are
    the code-owned RBAC role names — never free-form caller strings.

    Name-keying is safe because seeded role names are immutable
    (review R5): no application path updates roles.name
    (application/rbac.py mutates permissions only) and migration 0017
    ships a DB trigger refusing renames of is_system roles, so a
    rename can never silently detach a configured ceiling.
    """

    authority: str
    max_amount: Decimal | None


def _parse_money(value: object, *, where: str) -> Decimal:
    """Parse a band amount: Decimal, 2dp max, within NUMERIC(18,2)."""
    if isinstance(value, bool) or not isinstance(value, str | int | Decimal):
        raise BandConfigError(f"{where}: amount must be a decimal string or integer")
    try:
        amount = Decimal(str(value))
    except InvalidOperation as exc:
        raise BandConfigError(f"{where}: amount is not a valid decimal") from exc
    if not amount.is_finite():
        raise BandConfigError(f"{where}: amount must be finite")
    if amount != amount.quantize(Decimal("0.01")):
        raise BandConfigError(f"{where}: amount precision exceeds 2 decimal places")
    if amount < 0 or amount > MAX_MONEY:
        raise BandConfigError(f"{where}: amount outside the NUMERIC(18,2) domain")
    return amount


def _require_keys(entry: object, keys: frozenset[str], *, where: str) -> dict[str, object]:
    if not isinstance(entry, dict):
        raise BandConfigError(f"{where}: band must be an object")
    if set(entry.keys()) != keys:
        raise BandConfigError(f"{where}: band must carry exactly the keys {sorted(keys)}")
    return entry


_RATE_BAND_KEYS = frozenset({"min_amount", "max_amount", "rate_pct"})
_APPROVAL_BAND_KEYS = frozenset({"authority", "max_amount"})


def validate_loan_rate_bands(raw: object) -> tuple[RateBand, ...]:
    """Validate tiered loan-rate bands; the write AND read contract.

    Rules (each rejection is separately tested and falsifiable):
      * a non-empty JSON array of <= MAX_RATE_BANDS band objects
      * every band carries exactly {min_amount, max_amount, rate_pct}
      * first band's min_amount is 0 (contiguous from zero)
      * each band's min_amount equals the previous band's max_amount
        (no gaps, no overlaps by construction of the check)
      * max_amount strictly greater than min_amount (no inversions),
        strictly increasing across bands; NULL (open) only on the last
      * rate_pct in (0, 100], 2dp — the NUMERIC(5,2)/CHECK domain
    """
    if not isinstance(raw, list):
        raise BandConfigError("loan_rate_bands: must be a JSON array")
    if not raw:
        raise BandConfigError("loan_rate_bands: at least one band is required")
    if len(raw) > MAX_RATE_BANDS:
        raise BandConfigError(f"loan_rate_bands: at most {MAX_RATE_BANDS} bands")
    bands: list[RateBand] = []
    previous_max: Decimal = Decimal("0")
    for index, entry_raw in enumerate(raw):
        where = f"loan_rate_bands[{index}]"
        entry = _require_keys(entry_raw, _RATE_BAND_KEYS, where=where)
        min_amount = _parse_money(entry["min_amount"], where=where)
        if index == 0:
            if min_amount != 0:
                raise BandConfigError(f"{where}: bands must start at 0")
        elif min_amount != previous_max:
            raise BandConfigError(f"{where}: bands must be contiguous (gap or overlap detected)")
        max_raw = entry["max_amount"]
        max_amount: Decimal | None = None
        if max_raw is not None:
            max_amount = _parse_money(max_raw, where=where)
            if max_amount <= min_amount:
                raise BandConfigError(f"{where}: max_amount must exceed min_amount")
        elif index != len(raw) - 1:
            raise BandConfigError(f"{where}: only the last band may be open-ended")
        rate = _parse_money(entry["rate_pct"], where=where)
        if rate <= 0 or rate > MAX_PERCENT:
            raise BandConfigError(f"{where}: rate_pct must be in (0, 100]")
        bands.append(RateBand(min_amount=min_amount, max_amount=max_amount, rate_pct=rate))
        previous_max = max_amount if max_amount is not None else MAX_MONEY
    return tuple(bands)


def validate_approval_bands(raw: object) -> tuple[ApprovalBand, ...]:
    """Validate the approval matrix bands; the write AND read contract.

    Rules (each rejection is separately tested and falsifiable):
      * a non-empty JSON array of at most len(ROLE_NAMES) band objects
      * every band carries exactly {authority, max_amount}
      * authority is a code-owned RBAC role name, unique across bands
      * max_amount strictly increasing (non-overlapping ranges by
        construction); NULL (unbounded) only on the last band
    A finite top band is legal: amounts above it resolve to no listed
    authority (the explicit top-band rule — the prototype's "Board
    above" tier, which is not a platform role).
    """
    if not isinstance(raw, list):
        raise BandConfigError("approval_bands: must be a JSON array")
    if not raw:
        raise BandConfigError("approval_bands: at least one band is required")
    if len(raw) > len(ROLE_NAMES):
        raise BandConfigError(f"approval_bands: at most {len(ROLE_NAMES)} bands")
    bands: list[ApprovalBand] = []
    seen: set[str] = set()
    previous_max: Decimal | None = None
    for index, entry_raw in enumerate(raw):
        where = f"approval_bands[{index}]"
        entry = _require_keys(entry_raw, _APPROVAL_BAND_KEYS, where=where)
        authority = entry["authority"]
        if not isinstance(authority, str) or authority not in ROLE_NAMES:
            raise BandConfigError(f"{where}: authority must be a known role name")
        if authority in seen:
            raise BandConfigError(f"{where}: duplicate authority")
        seen.add(authority)
        max_raw = entry["max_amount"]
        max_amount: Decimal | None = None
        if max_raw is not None:
            max_amount = _parse_money(max_raw, where=where)
            if max_amount <= 0:
                raise BandConfigError(f"{where}: max_amount must be positive")
            if previous_max is not None and max_amount <= previous_max:
                raise BandConfigError(f"{where}: max_amount must be strictly increasing")
        elif index != len(raw) - 1:
            raise BandConfigError(f"{where}: only the last band may be unbounded")
        bands.append(ApprovalBand(authority=authority, max_amount=max_amount))
        previous_max = max_amount
    return tuple(bands)


def required_band_index(amount: Decimal, bands: tuple[ApprovalBand, ...]) -> int:
    """Deterministic band resolution: the index whose band contains amount.

    Bands are validated (strictly increasing, at most one unbounded
    tail), so the FIRST band whose ceiling covers the amount is the
    unique containing band. Returns len(bands) when the amount exceeds
    a finite top band — the explicit "no listed authority" tier.
    Linear scan; n <= len(ROLE_NAMES) by validation (documented bound).
    """
    for index, band in enumerate(bands):
        if band.max_amount is None or amount <= band.max_amount:
            return index
    return len(bands)


def authority_may_ratify(role_name: str, amount: Decimal, bands: tuple[ApprovalBand, ...]) -> bool:
    """Whether a role may ratify (advance/approve) the given amount.

    A role LISTED in the matrix is capped by its own band ceiling. A
    role NOT listed in a configured matrix FAILS CLOSED WITH A FLOOR
    (review R2): it may ratify only amounts within the FIRST band's
    ceiling, so a misconfigured matrix (a role holding
    applications:edit that the tenant forgot to list) degrades to the
    smallest configured authority instead of silently uncapping a
    money ceiling. The uncapped pre-P13.7 fallback (BUILD_PROMPTS
    P13.7: "current constants as fallback defaults") applies only when
    NO matrix is configured at all — enforce_authority_band returns
    before ever calling this function in that case.
    """
    required = required_band_index(amount, bands)
    for index, band in enumerate(bands):
        if band.authority == role_name:
            return index >= required
    # Fail closed with a floor: an unlisted role holds band-0 authority.
    return required == 0


def rate_for_amount(amount: Decimal, bands: tuple[RateBand, ...]) -> Decimal | None:
    """Tiered rate lookup: the unique band containing the amount.

    Returns None above a finite top band (callers fall back to the
    product rate). Linear scan; n <= MAX_RATE_BANDS (documented bound).
    """
    for band in bands:
        if band.max_amount is None or amount <= band.max_amount:
            return band.rate_pct
    return None
