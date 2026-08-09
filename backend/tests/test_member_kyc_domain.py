"""Pure domain tests for member KYC schemas & document machine (P13.12).

No database required. Payload oracles are hand-written from the
prototype's wizard forms (tests/kyc_payloads.py), never captured from
the implementation (§4).
"""

import pytest

from genesis.domain.member_kyc import (
    DOCUMENT_CHECKLISTS,
    DocumentStatus,
    DocumentTypeError,
    InvalidDocumentTransitionError,
    ProfileValidationError,
    document_transition,
    validate_document_type,
    validate_profile,
)
from genesis.domain.members import MemberType
from kyc_payloads import PII_SENTINEL, VALID_PROFILES

ALL_TYPES = list(MemberType)


def test_valid_profiles_pass_for_their_own_type() -> None:
    for member_type in ALL_TYPES:
        validated = validate_profile(member_type, VALID_PROFILES[member_type])
        # Canonical JSON mode: dates become ISO strings, sections survive.
        assert set(validated) == set(VALID_PROFILES[member_type])


def test_wrong_type_payload_is_rejected_full_matrix() -> None:
    """Every payload against every OTHER member type must fail (4x3)."""
    for member_type in ALL_TYPES:
        for payload_type in ALL_TYPES:
            if payload_type is member_type:
                continue
            with pytest.raises(ProfileValidationError):
                validate_profile(member_type, VALID_PROFILES[payload_type])


def test_missing_section_is_rejected() -> None:
    payload = {k: v for k, v in VALID_PROFILES[MemberType.PERSON].items() if k != "next_of_kin"}
    with pytest.raises(ProfileValidationError):
        validate_profile(MemberType.PERSON, payload)


def test_rider_field_in_a_section_is_rejected() -> None:
    payload = {k: dict(v) for k, v in VALID_PROFILES[MemberType.PERSON].items()}
    payload["bio"]["approved_rate_pct"] = "0.01"  # smuggled rider (gate 1.6)
    with pytest.raises(ProfileValidationError):
        validate_profile(MemberType.PERSON, payload)


def test_validation_error_never_echoes_submitted_values() -> None:
    """PII discipline (gate 1.6): locations and types only, no values."""
    with pytest.raises(ProfileValidationError) as excinfo:
        validate_profile(MemberType.COMPANY, VALID_PROFILES[MemberType.PERSON])
    assert PII_SENTINEL not in str(excinfo.value)
    assert "Wanjiku" not in str(excinfo.value)

    bad = {k: dict(v) for k, v in VALID_PROFILES[MemberType.PERSON].items()}
    bad["employment"]["monthly_income"] = f"secret-{PII_SENTINEL}"
    with pytest.raises(ProfileValidationError) as excinfo:
        validate_profile(MemberType.PERSON, bad)
    assert PII_SENTINEL not in str(excinfo.value)


def test_monthly_income_rejects_non_decimal_grammar() -> None:
    for bad_amount in ("1e5", "-100", "100.123", "NaN", "1,000"):
        payload = {k: dict(v) for k, v in VALID_PROFILES[MemberType.PERSON].items()}
        payload["employment"]["monthly_income"] = bad_amount
        with pytest.raises(ProfileValidationError):
            validate_profile(MemberType.PERSON, payload)


def test_document_checklists_match_prototype_and_validate() -> None:
    # Hand-copied from the prototype docsFor map (§4 oracle).
    assert DOCUMENT_CHECKLISTS[MemberType.PERSON] == frozenset(
        {"national_id_copy", "kra_pin", "passport_photo", "signature", "next_of_kin_id"}
    )
    assert DOCUMENT_CHECKLISTS[MemberType.VEHICLE] == frozenset(
        {"logbook", "insurance_cert", "ntsa_inspection", "psv_tlb_licence", "sacco_agreement"}
    )
    assert validate_document_type(MemberType.PERSON, "kra_pin") == "kra_pin"
    with pytest.raises(DocumentTypeError):
        validate_document_type(MemberType.PERSON, "logbook")
    with pytest.raises(DocumentTypeError):
        validate_document_type(MemberType.VEHICLE, "passport_photo")


def test_document_transition_full_matrix() -> None:
    allowed = {
        (DocumentStatus.PENDING, DocumentStatus.RECEIVED),
        (DocumentStatus.RECEIVED, DocumentStatus.VERIFIED),
        (DocumentStatus.RECEIVED, DocumentStatus.REJECTED),
        (DocumentStatus.REJECTED, DocumentStatus.RECEIVED),
    }
    for current in DocumentStatus:
        for target in DocumentStatus:
            if (current, target) in allowed:
                assert document_transition(current, target) is target
            else:
                with pytest.raises(InvalidDocumentTransitionError):
                    document_transition(current, target)
