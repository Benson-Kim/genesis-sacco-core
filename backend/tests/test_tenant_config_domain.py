"""P13.7 tenant-configuration domain suite (no I/O).

Covers the typed setting registry drift gates and the band contracts:

* registry <-> API model drift: the settings API accepts EXACTLY the
  code-owned registry keys (plus version) and returns exactly the
  registry keys (plus configured/version) — an unknown key can never
  be smuggled in and a registered key can never silently vanish.
* tiered loan-rate bands: property-style acceptance of arbitrary valid
  contiguous band lists, plus one falsifiable rejection test PER rule
  (overlap, gap, inversion, non-zero start, open band not last, rate
  bounds, precision, type). Remove any single check in
  validate_loan_rate_bands and its test below fails.
* approval matrix bands: same falsifiability discipline, plus the
  deterministic-resolution property (every amount resolves to exactly
  one band index or the explicit above-top tier).

Oracles are hand-computed in comments — never captured from the
implementation under test (MASTER_PROMPT §4).
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from genesis.domain.rbac import ROLE_NAMES
from genesis.domain.tenant_config import (
    MAX_RATE_BANDS,
    SETTINGS_REGISTRY,
    BandConfigError,
    authority_may_ratify,
    rate_for_amount,
    required_band_index,
    validate_approval_bands,
    validate_loan_rate_bands,
)

# ---------------------------------------------------------------------------
# Registry drift gates
# ---------------------------------------------------------------------------


def test_update_body_fields_match_registry_exactly() -> None:
    """The API body accepts exactly the registry keys + version."""
    from genesis.api.tenant_settings import SettingsUpdateBody

    assert set(SettingsUpdateBody.model_fields) == set(SETTINGS_REGISTRY) | {"version"}


def test_settings_out_fields_match_registry_exactly() -> None:
    from genesis.api.tenant_settings import SettingsOut

    assert set(SettingsOut.model_fields) == set(SETTINGS_REGISTRY) | {
        "configured",
        "version",
        "corrupt_keys",
    }


def test_every_registry_key_declares_type_unit_bounds_and_consumers() -> None:
    for key, spec in SETTINGS_REGISTRY.items():
        assert spec.key == key
        assert spec.value_type, key
        assert spec.unit, key
        assert spec.bounds, key
        assert spec.consumers, key


# ---------------------------------------------------------------------------
# Tiered loan-rate bands
# ---------------------------------------------------------------------------

_VALID_RATE_BANDS = [
    {"min_amount": "0", "max_amount": "100000.00", "rate_pct": "10.00"},
    {"min_amount": "100000.00", "max_amount": "500000.00", "rate_pct": "12.00"},
    {"min_amount": "500000.00", "max_amount": None, "rate_pct": "14.00"},
]


def test_rate_bands_valid_config_accepted() -> None:
    bands = validate_loan_rate_bands(_VALID_RATE_BANDS)
    assert len(bands) == 3
    # Hand-computed containment ((lower, upper] convention):
    # 100000.00 sits in band 0 (inclusive upper); 100000.01 in band 1;
    # 600000 in the open tail band.
    assert rate_for_amount(Decimal("100000.00"), bands) == Decimal("10.00")
    assert rate_for_amount(Decimal("100000.01"), bands) == Decimal("12.00")
    assert rate_for_amount(Decimal("600000"), bands) == Decimal("14.00")


def test_rate_bands_closed_top_band_returns_none_above() -> None:
    bands = validate_loan_rate_bands(
        [{"min_amount": "0", "max_amount": "100000.00", "rate_pct": "10.00"}]
    )
    # Above a finite top band there is no configured rate: callers fall
    # back to the product rate (documented open-tier rule).
    assert rate_for_amount(Decimal("100000.01"), bands) is None


@pytest.mark.parametrize(
    ("raw", "reason"),
    [
        ("not-a-list", "must be a JSON array"),
        ([], "at least one band"),
        ([{"min_amount": "0", "rate_pct": "10.00"}], "exactly the keys"),
        (
            [{"min_amount": "0", "max_amount": "1.00", "rate_pct": "10.00", "x": 1}],
            "exactly the keys",
        ),
        # Non-zero start: contiguity from zero is required.
        ([{"min_amount": "10.00", "max_amount": None, "rate_pct": "10.00"}], "start at 0"),
        # Gap: band 1 starts above band 0's upper bound.
        (
            [
                {"min_amount": "0", "max_amount": "100.00", "rate_pct": "10.00"},
                {"min_amount": "150.00", "max_amount": None, "rate_pct": "12.00"},
            ],
            "contiguous",
        ),
        # Overlap: band 1 starts below band 0's upper bound.
        (
            [
                {"min_amount": "0", "max_amount": "100.00", "rate_pct": "10.00"},
                {"min_amount": "50.00", "max_amount": None, "rate_pct": "12.00"},
            ],
            "contiguous",
        ),
        # Inversion: upper bound not above lower bound.
        (
            [
                {"min_amount": "0", "max_amount": "100.00", "rate_pct": "10.00"},
                {"min_amount": "100.00", "max_amount": "100.00", "rate_pct": "12.00"},
            ],
            "must exceed",
        ),
        # Open band in a non-terminal position.
        (
            [
                {"min_amount": "0", "max_amount": None, "rate_pct": "10.00"},
                {"min_amount": "100.00", "max_amount": "200.00", "rate_pct": "12.00"},
            ],
            "only the last band",
        ),
        # Rate bounds: (0, 100].
        ([{"min_amount": "0", "max_amount": None, "rate_pct": "0"}], "(0, 100]"),
        ([{"min_amount": "0", "max_amount": None, "rate_pct": "100.01"}], "(0, 100]"),
        # Precision and type discipline: floats and 3dp are refused,
        # never rounded (money contract).
        ([{"min_amount": 0.5, "max_amount": None, "rate_pct": "10.00"}], "decimal string"),
        ([{"min_amount": "0.005", "max_amount": None, "rate_pct": "10.00"}], "precision"),
        ([{"min_amount": "-1.00", "max_amount": None, "rate_pct": "10.00"}], "domain"),
        ([{"min_amount": "0", "max_amount": "NaN", "rate_pct": "10.00"}], "finite"),
        ([{"min_amount": True, "max_amount": None, "rate_pct": "10.00"}], "decimal string"),
    ],
)
def test_rate_bands_rejections(raw: object, reason: str) -> None:
    with pytest.raises(BandConfigError, match="loan_rate_bands"):
        validate_loan_rate_bands(raw)
    del reason  # the match target documents the rule under test


def test_rate_bands_count_cap() -> None:
    bands = []
    lower = Decimal("0")
    for _ in range(MAX_RATE_BANDS + 1):
        upper = lower + 100
        bands.append({"min_amount": str(lower), "max_amount": str(upper), "rate_pct": "10.00"})
        lower = upper
    bands[-1]["max_amount"] = None
    with pytest.raises(BandConfigError, match="at most"):
        validate_loan_rate_bands(bands)


@given(
    bounds=st.lists(
        st.integers(min_value=1, max_value=10_000_000), min_size=1, max_size=6, unique=True
    ),
    probe=st.integers(min_value=0, max_value=20_000_000),
)
def test_rate_bands_property_valid_contiguous_lists_accepted(bounds: list[int], probe: int) -> None:
    """Property: any strictly-increasing contiguous list validates, and
    every probe amount resolves to at most one band (the containment
    scan finds the FIRST match; contiguity makes it the ONLY match)."""
    uppers = sorted(bounds)
    raw: list[dict[str, object]] = []
    lower = 0
    for upper in uppers:
        raw.append({"min_amount": str(lower), "max_amount": str(upper), "rate_pct": "10.00"})
        lower = upper
    bands = validate_loan_rate_bands(raw)
    amount = Decimal(probe)
    containing = [
        b
        for b in bands
        if b.min_amount < amount and (b.max_amount is None or amount <= b.max_amount)
    ]
    assert len(containing) <= 1
    if containing:
        assert rate_for_amount(amount, bands) == containing[0].rate_pct


# ---------------------------------------------------------------------------
# Approval matrix bands
# ---------------------------------------------------------------------------

# The prototype matrix: LO <= 100k, BM <= 500k, CC <= 2M, System Admin
# unbounded (the platform's stand-in for the prototype "Board" tier).
_PROTO_MATRIX = [
    {"authority": "Loan Officer", "max_amount": "100000.00"},
    {"authority": "Branch Manager", "max_amount": "500000.00"},
    {"authority": "Credit Committee", "max_amount": "2000000.00"},
    {"authority": "System Admin", "max_amount": None},
]


def test_approval_bands_valid_config_and_resolution() -> None:
    bands = validate_approval_bands(_PROTO_MATRIX)
    # Hand-computed band resolution ((lower, upper] convention):
    #   50 000    -> band 0 (<= 100k)
    #   100 000   -> band 0 (inclusive upper bound)
    #   100 000.01-> band 1
    #   2 000 000 -> band 2
    #   2 000 001 -> band 3 (open tail)
    assert required_band_index(Decimal("50000"), bands) == 0
    assert required_band_index(Decimal("100000.00"), bands) == 0
    assert required_band_index(Decimal("100000.01"), bands) == 1
    assert required_band_index(Decimal("2000000.00"), bands) == 2
    assert required_band_index(Decimal("2000001.00"), bands) == 3


def test_approval_bands_authority_ceilings() -> None:
    bands = validate_approval_bands(_PROTO_MATRIX)
    # An authority covers its own band and every band below it.
    assert authority_may_ratify("Loan Officer", Decimal("100000.00"), bands)
    assert not authority_may_ratify("Loan Officer", Decimal("100000.01"), bands)
    assert authority_may_ratify("Branch Manager", Decimal("100000.01"), bands)
    assert authority_may_ratify("Credit Committee", Decimal("50000"), bands)
    assert not authority_may_ratify("Credit Committee", Decimal("2000000.01"), bands)
    assert authority_may_ratify("System Admin", Decimal("99999999.00"), bands)
    # Unlisted roles fail closed with a floor (review R2): they may
    # ratify only within the FIRST band's ceiling (100 000 here) — a
    # forgotten role degrades to the smallest configured authority,
    # never to an uncapped ceiling. Fails in the second direction if
    # the fail-open `return True` is restored.
    assert authority_may_ratify("Auditor", Decimal("100000.00"), bands)
    assert not authority_may_ratify("Auditor", Decimal("100000.01"), bands)
    assert not authority_may_ratify("Auditor", Decimal("99999999.00"), bands)


def test_approval_bands_finite_top_band_blocks_all_listed_roles() -> None:
    """Explicit top-band rule: above a finite top band, NO listed
    authority may ratify (the prototype's 'Board above' tier)."""
    bands = validate_approval_bands(
        [
            {"authority": "Loan Officer", "max_amount": "100000.00"},
            {"authority": "Credit Committee", "max_amount": "2000000.00"},
        ]
    )
    assert required_band_index(Decimal("2000000.01"), bands) == len(bands)
    assert not authority_may_ratify("Loan Officer", Decimal("2000000.01"), bands)
    assert not authority_may_ratify("Credit Committee", Decimal("2000000.01"), bands)


@pytest.mark.parametrize(
    ("raw", "reason"),
    [
        ({"authority": "Loan Officer"}, "must be a JSON array"),
        ([], "at least one band"),
        ([{"authority": "Loan Officer"}], "exactly the keys"),
        # Free-form authority strings are refused: authorities are the
        # code-owned role names (typed-registry rule).
        ([{"authority": "Board", "max_amount": None}], "known role name"),
        (
            [
                {"authority": "Loan Officer", "max_amount": "100.00"},
                {"authority": "Loan Officer", "max_amount": None},
            ],
            "duplicate",
        ),
        # Non-increasing ceilings would make band resolution ambiguous.
        (
            [
                {"authority": "Loan Officer", "max_amount": "500.00"},
                {"authority": "Branch Manager", "max_amount": "500.00"},
            ],
            "strictly increasing",
        ),
        (
            [
                {"authority": "Loan Officer", "max_amount": None},
                {"authority": "Branch Manager", "max_amount": "500.00"},
            ],
            "only the last band",
        ),
        ([{"authority": "Loan Officer", "max_amount": "0"}], "positive"),
        ([{"authority": "Loan Officer", "max_amount": 100.5}], "decimal string"),
    ],
)
def test_approval_bands_rejections(raw: object, reason: str) -> None:
    with pytest.raises(BandConfigError, match="approval_bands"):
        validate_approval_bands(raw)
    del reason


def test_approval_bands_at_most_one_per_role() -> None:
    raw = [
        {"authority": name, "max_amount": str((i + 1) * 1000)} for i, name in enumerate(ROLE_NAMES)
    ]
    assert len(validate_approval_bands(raw)) == len(ROLE_NAMES)


@given(
    count=st.integers(min_value=1, max_value=len(ROLE_NAMES)),
    bounds=st.lists(
        st.integers(min_value=1, max_value=10_000_000),
        min_size=len(ROLE_NAMES),
        max_size=len(ROLE_NAMES),
        unique=True,
    ),
    probe=st.integers(min_value=0, max_value=20_000_000),
    open_tail=st.booleans(),
)
def test_approval_bands_property_resolution_is_deterministic(
    count: int, bounds: list[int], probe: int, open_tail: bool
) -> None:
    """Property: for any valid matrix, an amount resolves to exactly one
    band index in [0, len] and the FIRST covering band is that index."""
    uppers = sorted(bounds)[:count]
    raw: list[dict[str, object]] = [
        {"authority": ROLE_NAMES[i], "max_amount": str(uppers[i])} for i in range(count)
    ]
    if open_tail:
        raw[-1]["max_amount"] = None
    bands = validate_approval_bands(raw)
    amount = Decimal(probe)
    index = required_band_index(amount, bands)
    assert 0 <= index <= len(bands)
    covering = [i for i, b in enumerate(bands) if b.max_amount is None or amount <= b.max_amount]
    # Strictly increasing ceilings mean the covering set is a suffix;
    # its first element is the unique resolution.
    assert index == (covering[0] if covering else len(bands))
