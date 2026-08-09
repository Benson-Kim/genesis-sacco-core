"""Shared hand-written KYC profile payloads (P13.12; not a test module).

Oracles are HAND-COMPUTED from the prototype's add-member wizard forms
(genesis_prestige_app.html `forms` map) — never captured from the
implementation under test (§4).
"""

from typing import Any

from genesis.domain.members import MemberType

#: Sentinel PII value used to assert nothing echoes submitted values.
PII_SENTINEL = "38442211"

VALID_PROFILES: dict[MemberType, dict[str, Any]] = {
    MemberType.PERSON: {
        "bio": {
            "first_name": "Wanjiku",
            "surname": "Kamau",
            "gender": "female",
            "date_of_birth": "1990-04-12",
            "id_number": PII_SENTINEL,
            "kra_pin": "A012345678B",
        },
        "contact": {
            "phone": "+254700000001",
            "email": "wanjiku@example.com",
            "county": "Nairobi",
            "physical_address": "Moi Avenue 12, Nairobi",
        },
        "employment": {
            "employment_status": "employed",
            "occupation": "Teacher",
            "monthly_income": "85000.00",
        },
        "next_of_kin": {
            "name": "James Kamau",
            "relationship": "spouse",
            "phone": "+254700000002",
        },
    },
    MemberType.COMPANY: {
        "registration": {
            "registered_name": "Acme Traders Ltd",
            "reg_number": "PVT-2020-1234",
            "kra_pin": "P051234567Q",
            "incorporated_on": "2020-02-01",
            "industry": "Retail",
            "nature_of_business": "General merchandise",
        },
        "office": {
            "address": "Kimathi Street 4",
            "town": "Nairobi",
            "county": "Nairobi",
        },
        "contact": {
            "contact_name": "Grace Njeri",
            "role": "Finance Manager",
            "phone": "+254711000001",
            "email": "finance@acme.example.com",
        },
        "signatories": [
            {
                "director_name": "Peter Otieno",
                "id_number": "11223344",
                "role": "Director",
            }
        ],
    },
    MemberType.GROUP: {
        "registration": {
            "group_name": "Umoja Welfare Chama",
            "group_type": "welfare",
            "reg_number": "SHG-2019-778",
            "date_formed": "2019-06-15",
            "county": "Kiambu",
            "members_count": 24,
        },
        "officials": {
            "chairperson": {
                "name": "Mary Wairimu",
                "id_number": "22334455",
                "phone": "+254722000001",
            },
            "secretary": {
                "name": "John Mwangi",
                "id_number": "33445566",
                "phone": "+254722000002",
            },
            "treasurer": {
                "name": "Alice Achieng",
                "id_number": "44556677",
                "phone": "+254722000003",
            },
        },
    },
    MemberType.VEHICLE: {
        "vehicle": {
            "registration_number": "KDA 123A",
            "make": "Toyota",
            "model": "Hiace",
            "year": 2019,
            "body_type": "matatu",
            "passenger_capacity": 14,
        },
        "compliance": {
            "route": "Route 46 — CBD to Kawangware",
            "tlb_psv_licence": "PSV-889900",
            "ntsa_inspection_expiry": "2026-11-30",
            "insurance_provider": "Jubilee",
            "insurance_expiry": "2026-09-30",
        },
        "ownership": {
            "registered_owner": "Samuel Kariuki",
            "ownership_type": "individual",
            "financier": None,
        },
    },
}
