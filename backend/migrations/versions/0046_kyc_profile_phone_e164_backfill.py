"""KYC profile phone local-format -> E.164 backfill (issue #35 follow-through)

Revision ID: 0046
Revises: 0045
Create Date: 2026-08-09

DATA-ONLY revision (the migration-declaration rule, claimed in the MR
description at branch time; chained onto 0045 — the id-number-lookup
MR of the same session — and declared as a stacked dependency there).
The 0042 round made E.164 (+254…) the canonical storage for every
Kenya mobile identifier and backfilled members.phone / users.phone;
the KYC jsonb profile phones were recorded then as the honest residual
(the !87 close-out). The write path now normalizes every profile phone
through the ONE existing normalizer
(domain/members.normalize_kenya_msisdn via the KenyaMsisdn field type
in domain/member_kyc.py); this revision migrates the profile rows that
predate it.

The SIX code-owned jsonb paths that carry a phone (the per-type
profile schemas in domain/member_kyc.py — nothing else in a profile is
a phone):

  * person : {contact,phone}, {next_of_kin,phone}
  * company: {contact,phone}
  * group  : {officials,chairperson,phone}, {officials,secretary,phone},
             {officials,treasurer,phone}
  * vehicle: none.

Scope is deliberately EXACT (the 0042 discipline): only values
matching the full local format ^0[71][0-9]{8}$ are rewritten (anchored
— no digit harvesting; any other shape is left untouched and
read-tolerated). The rewrite is a pure prefix substitution
0X… -> +254X…, losslessly invertible, executed with jsonb_set on the
one named path per statement — no other profile key can be touched.
IDEMPOTENT BY CONSTRUCTION: a rewritten value no longer matches the
predicate, so a second run changes ZERO rows (falsifiable row-count
legs in tests/test_kyc_phone_e164.py).

NO schema, constraint, index or RLS change: bounded UPDATEs on the
small member_profiles table, safe in one transaction. Old code reads
E.164 profile values fine (the profile is opaque jsonb to it), so the
change is backward-compatible one release.

Downgrade is the EXACT inverse: values matching ^\\+254[71][0-9]{8}$
on the same six paths are rewritten back to the local 0X… form. This
restores the pre-upgrade representation for every row the upgrade
rewrote; an E.164 value written natively by the new code is also
rewritten, which remains a valid accepted input format for the new
write path (the documented 0042 trade-off — the two populations are
indistinguishable by construction).
"""

from alembic import op

revision = "0046"
down_revision = "0045"
branch_labels = None
depends_on = None


def _rewrite(member_type: str, path: str, *, up: bool) -> str:
    """One bounded UPDATE for one code-owned profile path.

    ``path`` is a code-owned literal from the schema map above — never
    caller input — so the interpolation is injection-safe by
    construction (and this module runs only inside alembic).
    """
    if up:
        expr = f"to_jsonb('+254' || substr(profile #>> '{{{path}}}', 2))"
        predicate = f"profile #>> '{{{path}}}' ~ '^0[71][0-9]{{8}}$'"
    else:
        expr = f"to_jsonb('0' || substr(profile #>> '{{{path}}}', 5))"
        predicate = f"profile #>> '{{{path}}}' ~ '^\\+254[71][0-9]{{8}}$'"
    return (  # noqa: S608
        "UPDATE member_profiles\n"
        f"   SET profile = jsonb_set(profile, '{{{path}}}', {expr})\n"
        f" WHERE member_type = '{member_type}'\n"
        f"   AND {predicate};"
    )


#: (member_type, jsonb path) — the exhaustive code-owned phone map.
_PHONE_PATHS: tuple[tuple[str, str], ...] = (
    ("person", "contact,phone"),
    ("person", "next_of_kin,phone"),
    ("company", "contact,phone"),
    ("group", "officials,chairperson,phone"),
    ("group", "officials,secretary,phone"),
    ("group", "officials,treasurer,phone"),
)

_UP = "\n".join(_rewrite(member_type, path, up=True) for member_type, path in _PHONE_PATHS)

_DOWN = "\n".join(_rewrite(member_type, path, up=False) for member_type, path in _PHONE_PATHS)


def upgrade() -> None:
    op.execute(_UP)


def downgrade() -> None:
    op.execute(_DOWN)
