"""Member KYC domain: per-type profile schemas, document checklists and
the document status machine (P13.12, gates 1.4-1.6). Zero I/O.

The prototype's add-member wizard collects type-specific detail forms
(GAP_ANALYSIS §2.3). Their schemas live here as the single source of
truth: pydantic models with ``extra="forbid"`` mirror the prototype's
sections field-for-field, so a payload of the wrong member type — or a
rider field smuggled into any section — is rejected at validation time.
The DB CHECK in migration 0018 backstops the section shape for raw SQL.

PII discipline (gate 1.6): validation failures raise
ProfileValidationError carrying only field LOCATIONS and error TYPES —
never the submitted values — so KYC data can never leak through error
messages or logs.

Monetary-looking KYC fields (monthly income) are informational strings
constrained to the decimal grammar — never float (MASTER_PROMPT §0),
and never inputs to any money computation.
"""

from __future__ import annotations

import enum
from collections.abc import Mapping
from datetime import date
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from genesis.domain.members import MemberType

#: Informational money amounts (KYC only): decimal grammar, never float.
_AMOUNT_PATTERN = r"^\d{1,16}(\.\d{1,2})?$"


class ProfileValidationError(Exception):
    """Profile payload does not match the member type's schema.

    The message names field locations and error kinds only — submitted
    values are deliberately excluded (gate 1.6).
    """


class DocumentTypeError(Exception):
    """Document type is not on the member type's checklist."""


class InvalidDocumentTransitionError(Exception):
    """Raised when a document status transition is not allowed (gate 1.4)."""


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


# ---------------------------------------------------------------------------
# Person — bio / contact / employment / next of kin
# ---------------------------------------------------------------------------


class PersonBio(_Section):
    first_name: str = Field(min_length=1, max_length=100)
    surname: str = Field(min_length=1, max_length=100)
    gender: str = Field(min_length=1, max_length=20)
    date_of_birth: date
    id_number: str = Field(min_length=1, max_length=20)
    kra_pin: str = Field(min_length=1, max_length=20)


class PersonContact(_Section):
    phone: str = Field(min_length=1, max_length=32)
    email: str | None = Field(default=None, max_length=254)
    county: str = Field(min_length=1, max_length=60)
    physical_address: str = Field(min_length=1, max_length=200)


class PersonEmployment(_Section):
    employment_status: str = Field(min_length=1, max_length=60)
    occupation: str = Field(min_length=1, max_length=100)
    monthly_income: str = Field(pattern=_AMOUNT_PATTERN)


class PersonNextOfKin(_Section):
    name: str = Field(min_length=1, max_length=200)
    relationship: str = Field(min_length=1, max_length=60)
    phone: str = Field(min_length=1, max_length=32)


class PersonProfile(_Section):
    bio: PersonBio
    contact: PersonContact
    employment: PersonEmployment
    next_of_kin: PersonNextOfKin


# ---------------------------------------------------------------------------
# Company — registration / office / contact / signatories
# ---------------------------------------------------------------------------


class CompanyRegistration(_Section):
    registered_name: str = Field(min_length=1, max_length=200)
    reg_number: str = Field(min_length=1, max_length=60)
    kra_pin: str = Field(min_length=1, max_length=20)
    incorporated_on: date
    industry: str = Field(min_length=1, max_length=100)
    nature_of_business: str = Field(min_length=1, max_length=200)


class CompanyOffice(_Section):
    address: str = Field(min_length=1, max_length=200)
    town: str = Field(min_length=1, max_length=60)
    county: str = Field(min_length=1, max_length=60)


class CompanyContact(_Section):
    contact_name: str = Field(min_length=1, max_length=200)
    role: str = Field(min_length=1, max_length=60)
    phone: str = Field(min_length=1, max_length=32)
    email: str | None = Field(default=None, max_length=254)


class CompanySignatory(_Section):
    director_name: str = Field(min_length=1, max_length=200)
    id_number: str = Field(min_length=1, max_length=20)
    role: str = Field(min_length=1, max_length=60)


class CompanyProfile(_Section):
    registration: CompanyRegistration
    office: CompanyOffice
    contact: CompanyContact
    signatories: list[CompanySignatory] = Field(min_length=1, max_length=10)


# ---------------------------------------------------------------------------
# Group — registration / officials (chairperson, secretary, treasurer)
# ---------------------------------------------------------------------------


class GroupRegistration(_Section):
    group_name: str = Field(min_length=1, max_length=200)
    group_type: str = Field(min_length=1, max_length=60)
    reg_number: str = Field(min_length=1, max_length=60)
    date_formed: date
    county: str = Field(min_length=1, max_length=60)
    members_count: int = Field(ge=1, le=100_000)


class GroupOfficial(_Section):
    name: str = Field(min_length=1, max_length=200)
    id_number: str = Field(min_length=1, max_length=20)
    phone: str = Field(min_length=1, max_length=32)


class GroupOfficials(_Section):
    chairperson: GroupOfficial
    secretary: GroupOfficial
    treasurer: GroupOfficial


class GroupProfile(_Section):
    registration: GroupRegistration
    officials: GroupOfficials


# ---------------------------------------------------------------------------
# Vehicle — vehicle / compliance / ownership
# ---------------------------------------------------------------------------


class VehicleDetails(_Section):
    registration_number: str = Field(min_length=1, max_length=20)
    make: str = Field(min_length=1, max_length=60)
    model: str = Field(min_length=1, max_length=60)
    year: int = Field(ge=1950, le=2100)
    body_type: str = Field(min_length=1, max_length=60)
    passenger_capacity: int = Field(ge=1, le=200)


class VehicleCompliance(_Section):
    route: str = Field(min_length=1, max_length=100)
    tlb_psv_licence: str = Field(min_length=1, max_length=60)
    ntsa_inspection_expiry: date
    insurance_provider: str = Field(min_length=1, max_length=100)
    insurance_expiry: date


class VehicleOwnership(_Section):
    registered_owner: str = Field(min_length=1, max_length=200)
    ownership_type: str = Field(min_length=1, max_length=60)
    financier: str | None = Field(default=None, max_length=200)


class VehicleProfile(_Section):
    vehicle: VehicleDetails
    compliance: VehicleCompliance
    ownership: VehicleOwnership


#: Code-owned type -> schema map; single source of truth for validation.
PROFILE_MODELS: Mapping[MemberType, type[_Section]] = {
    MemberType.PERSON: PersonProfile,
    MemberType.COMPANY: CompanyProfile,
    MemberType.GROUP: GroupProfile,
    MemberType.VEHICLE: VehicleProfile,
}


def validate_profile(member_type: MemberType, payload: Mapping[str, Any]) -> dict[str, Any]:
    """Validate a profile payload against the member type's schema.

    Returns the canonical JSON-mode dict (dates as ISO strings) for
    persistence. Raises ProfileValidationError naming field locations
    and error types only — never the submitted values (gate 1.6).
    """
    model = PROFILE_MODELS[member_type]
    try:
        parsed = model.model_validate(dict(payload))
    except ValidationError as exc:
        problems = ", ".join(
            f"{'.'.join(str(part) for part in error['loc']) or '<root>'}: {error['type']}"
            for error in exc.errors()
        )
        raise ProfileValidationError(
            f"profile payload does not match schema for member type "
            f"{member_type.value} ({problems})"
        ) from exc
    return parsed.model_dump(mode="json")


# ---------------------------------------------------------------------------
# Document checklists (prototype docsFor) and status machine
# ---------------------------------------------------------------------------

#: Code-owned per-type checklist; mirrored by the 0018 DB CHECK.
DOCUMENT_CHECKLISTS: Mapping[MemberType, frozenset[str]] = {
    MemberType.PERSON: frozenset(
        {"national_id_copy", "kra_pin", "passport_photo", "signature", "next_of_kin_id"}
    ),
    MemberType.COMPANY: frozenset(
        {"cert_of_incorporation", "cr12", "kra_pin", "board_resolution", "signatory_ids"}
    ),
    MemberType.GROUP: frozenset(
        {"registration_cert", "constitution", "members_list", "resolution", "officials_ids"}
    ),
    MemberType.VEHICLE: frozenset(
        {"logbook", "insurance_cert", "ntsa_inspection", "psv_tlb_licence", "sacco_agreement"}
    ),
}


class DocumentStatus(enum.StrEnum):
    PENDING = "pending"
    RECEIVED = "received"
    VERIFIED = "verified"
    REJECTED = "rejected"


_DOC_ALLOWED: dict[DocumentStatus, frozenset[DocumentStatus]] = {
    DocumentStatus.PENDING: frozenset({DocumentStatus.RECEIVED}),
    DocumentStatus.RECEIVED: frozenset({DocumentStatus.VERIFIED, DocumentStatus.REJECTED}),
    DocumentStatus.REJECTED: frozenset({DocumentStatus.RECEIVED}),
    DocumentStatus.VERIFIED: frozenset(),
}


def validate_document_type(member_type: MemberType, doc_type: str) -> str:
    """Refuse a document type outside the member type's checklist."""
    if doc_type not in DOCUMENT_CHECKLISTS[member_type]:
        raise DocumentTypeError(
            f"document type {doc_type} is not on the {member_type.value} checklist"
        )
    return doc_type


def document_transition(current: DocumentStatus, target: DocumentStatus) -> DocumentStatus:
    """Validate a document status transition; raise on any illegal move.

    pending -> received (upload/handover), received -> verified |
    rejected (review), rejected -> received (resubmission); verified is
    terminal (gate 1.4).
    """
    if target not in _DOC_ALLOWED[current]:
        raise InvalidDocumentTransitionError(f"cannot move document from {current} to {target}")
    return target
