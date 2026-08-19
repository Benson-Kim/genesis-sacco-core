"""Detective controls on privileged money actions (issue #1 — the G5
first step; INSTITUTIONAL_GAP_REGISTER G2/G5).

The preventive stack already refuses what it can PROVE is wrong:
deny-by-default RBAC (api/authz.py), maker <> checker SoD
(application/sod.py + the 0031/0040 DB CHECKs), tenant-configured
authority bands (tenant_settings.enforce_authority_band) and the
committee quorum. What it cannot prove is an insider using CORRECT
credentials beyond their legitimate scope — clearing their own or a
relative's loan through the corrections channel. This module adds that
detective layer for the privileged money paths in
application/corrections.py (repayment adjustments, loan write-offs,
recovery receipts, misc fees).

Two postures, deliberately separated:

* ``enforce_no_self_dealing`` — a BLOCKING control (403, fail closed):
  a privileged actor whose verifiable identity surface (users.email /
  users.phone) matches the beneficiary member's identity surface
  (members.email / members.phone, or an ACTIVE member_credentials
  email — the 0035 "the link is the authority" table) may not move
  that member's money through the corrections channel; another officer
  must act. The raise rolls the whole transaction back (no partial
  success, 1.5), so the API layer records the refused ATTEMPT
  out-of-band (api/security_refusals.refusals_audited) — the attempt
  itself must leave a forensic audit row.
  RECORDED LIMITATION (not hidden): kinship ("a relative's loan") is
  detectable only through SHARED CONTACT DETAILS today — the KYC
  record holds no next-of-kin relations yet. When it does, the linkage
  probe below is the single place to extend (reuse-first, 1.1).

* ``emit_anomaly_signals`` — NON-BLOCKING telemetry (gate 1.2:
  analytics never breaks the action): off-hours privileged writes,
  branch-A-staff-acting-on-branch-B-member, and — on the paths that
  deliberately do not block (recovery receipts, fees) — actor↔member
  linkage. Triggered signals write a ``security.anomaly`` audit row IN
  the action's own transaction (the trail can never disagree with the
  data, 1.5) and enqueue a ``security.anomaly`` outbox event for
  alerting (side effects via the transactional outbox only, 1.2).
  Signal COMPUTATION failures are logged and swallowed; the
  audit/outbox WRITES share the action's integrity domain and are
  deliberately unguarded — if those inserts fail the transaction is
  already doomed regardless.

The queryable review surface is the existing audit-log register
(application/audit_log.py): the ``security.*`` action prefix filters
the stream, and payload disclosure follows the entity's module
entitlement exactly like every other audit row (least disclosure).
"""

from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from genesis.application.audit import record_audit
from genesis.application.outbox import enqueue_event
from genesis.errors import ForbiddenError

logger = logging.getLogger(__name__)


class AuditedEntity(StrEnum):
    """The code-owned vocabulary of workflow entities this module may
    audit against. The F4 static gate (tests/test_audit_entity_map.py)
    requires a LITERAL entity= at every record_audit call site, so
    ``write_security_audit`` below dispatches each enum member to its
    own literal call — deny by default: an entity outside this enum
    cannot be security-audited at all, and every member is pinned to
    ENTITY_MODULES by the same gate."""

    REPAYMENT_ADJUSTMENTS = "repayment_adjustments"
    LOAN_WRITE_OFFS = "loan_write_offs"
    LOAN_RECOVERIES = "loan_recoveries"
    TRANSACTIONS = "transactions"


async def write_security_audit(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID,
    *,
    action: str,
    entity: AuditedEntity,
    entity_id: str,
    after: dict[str, object],
) -> None:
    """One in-transaction security audit row, entity dispatched to a
    literal per the F4 static verifiability rule."""
    if entity is AuditedEntity.REPAYMENT_ADJUSTMENTS:
        await record_audit(
            session,
            tenant_id,
            actor_id,
            action=action,
            entity="repayment_adjustments",
            entity_id=entity_id,
            after=after,
        )
    elif entity is AuditedEntity.LOAN_WRITE_OFFS:
        await record_audit(
            session,
            tenant_id,
            actor_id,
            action=action,
            entity="loan_write_offs",
            entity_id=entity_id,
            after=after,
        )
    elif entity is AuditedEntity.LOAN_RECOVERIES:
        await record_audit(
            session,
            tenant_id,
            actor_id,
            action=action,
            entity="loan_recoveries",
            entity_id=entity_id,
            after=after,
        )
    else:
        await record_audit(
            session,
            tenant_id,
            actor_id,
            action=action,
            entity="transactions",
            entity_id=entity_id,
            after=after,
        )


# ---------------------------------------------------------------------------
# Signal vocabulary (code-owned; payloads carry these names, never PII)
# ---------------------------------------------------------------------------

#: Actor and beneficiary share an email (users.email vs members.email).
SIGNAL_SHARED_EMAIL = "shared_email"
#: Actor and beneficiary share a phone (users.phone vs members.phone,
#: compared on the normalized Kenyan MSISDN tail).
SIGNAL_SHARED_PHONE = "shared_phone"
#: Actor's email is an ACTIVE member credential of the beneficiary —
#: the strongest link: the 0035 table IS the member-identity authority.
SIGNAL_CREDENTIAL_LINK = "member_credential_email"
#: Privileged write outside the business window (below).
SIGNAL_OFF_HOURS = "off_hours"
#: Actor's home branch differs from the beneficiary member's branch
#: (both known — a NULL on either side is the honest "unassigned"
#: state and never fires; issue #8 branch-scoping groundwork).
SIGNAL_CROSS_BRANCH = "cross_branch"

#: The linkage signals that BLOCK on the enforced paths.
LINKAGE_SIGNALS: frozenset[str] = frozenset(
    {SIGNAL_SHARED_EMAIL, SIGNAL_SHARED_PHONE, SIGNAL_CREDENTIAL_LINK}
)

# ---------------------------------------------------------------------------
# Business window (Kenya has no DST: EAT is a fixed UTC+3 — no tzdata
# dependency). Code-owned defaults today; externalizing them follows
# the issue-#3 effective-dated parameter pattern when it lands.
# ---------------------------------------------------------------------------

_EAT_OFFSET = timedelta(hours=3)
BUSINESS_DAY_START_HOUR = 7  # 07:00 EAT
BUSINESS_DAY_END_HOUR = 19  # 19:00 EAT
_SUNDAY = 6  # datetime.weekday()


def _now() -> datetime:
    """Module-level clock seam (tests monkeypatch this, never real time)."""
    return datetime.now(UTC)


def is_off_hours(at_utc: datetime) -> bool:
    """True outside Mon-Sat 07:00-19:00 EAT (Sunday is always off-hours)."""
    local = at_utc.astimezone(UTC) + _EAT_OFFSET
    if local.weekday() == _SUNDAY:
        return True
    return not (BUSINESS_DAY_START_HOUR <= local.hour < BUSINESS_DAY_END_HOUR)


def normalize_phone(raw: str | None) -> str:
    """Kenyan MSISDN comparison tail: last 9 digits ('0712...' ==
    '+254712...'). Shorter digit runs can never match (returns '')."""
    if not raw:
        return ""
    digits = re.sub(r"\D", "", raw)
    return digits[-9:] if len(digits) >= 9 else ""


# ---------------------------------------------------------------------------
# The linkage probe
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LinkageProbe:
    """One evaluation of the actor↔member identity surface (no PII —
    only the signal names and branch ids ever leave this dataclass)."""

    linkage: tuple[str, ...]
    actor_branch_id: uuid.UUID | None
    member_branch_id: uuid.UUID | None

    @property
    def cross_branch(self) -> bool:
        return (
            self.actor_branch_id is not None
            and self.member_branch_id is not None
            and self.actor_branch_id != self.member_branch_id
        )


async def probe_linkage(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID,
    member_id: uuid.UUID,
) -> LinkageProbe:
    """Resolve the actor's and beneficiary's identity surfaces
    SERVER-SIDE (never from the JWT or a client flag — the sod.py
    posture) and compare them. Explicit tenant predicates double the
    forced RLS as everywhere; PK/index probes only."""
    actor_row = (
        await session.execute(
            text(
                "SELECT email, phone, branch_id FROM users "
                "WHERE id = CAST(:uid AS uuid) AND tenant_id = CAST(:tid AS uuid)"
            ),
            {"uid": str(actor_id), "tid": str(tenant_id)},
        )
    ).first()
    member_row = (
        await session.execute(
            text(
                "SELECT email, phone, branch_id FROM members "
                "WHERE id = CAST(:mid AS uuid) AND tenant_id = CAST(:tid AS uuid)"
            ),
            {"mid": str(member_id), "tid": str(tenant_id)},
        )
    ).first()
    if actor_row is None or member_row is None:
        # Fail closed on the CALLER's terms: the corrections services
        # have already 404'd unknown members and the authz dependency
        # unknown actors; an unresolvable principal here means the
        # probe cannot vouch for separation — treat as linked.
        return LinkageProbe(
            linkage=(SIGNAL_CREDENTIAL_LINK,),
            actor_branch_id=None,
            member_branch_id=None,
        )
    actor_email = (str(actor_row[0]).strip().lower()) if actor_row[0] else ""
    actor_phone = normalize_phone(str(actor_row[1]) if actor_row[1] else None)
    member_email = (str(member_row[0]).strip().lower()) if member_row[0] else ""
    member_phone = normalize_phone(str(member_row[1]) if member_row[1] else None)

    signals: list[str] = []
    if actor_email and member_email and actor_email == member_email:
        signals.append(SIGNAL_SHARED_EMAIL)
    if actor_phone and member_phone and actor_phone == member_phone:
        signals.append(SIGNAL_SHARED_PHONE)

    if actor_email:
        credential = (
            await session.execute(
                text(
                    "SELECT 1 FROM member_credentials "
                    "WHERE tenant_id = CAST(:tid AS uuid) "
                    "AND member_id = CAST(:mid AS uuid) "
                    "AND status = 'active' AND lower(email) = :email"
                ),
                {"tid": str(tenant_id), "mid": str(member_id), "email": actor_email},
            )
        ).first()
        if credential is not None:
            signals.append(SIGNAL_CREDENTIAL_LINK)

    return LinkageProbe(
        linkage=tuple(signals),
        actor_branch_id=uuid.UUID(str(actor_row[2])) if actor_row[2] is not None else None,
        member_branch_id=uuid.UUID(str(member_row[2])) if member_row[2] is not None else None,
    )


# ---------------------------------------------------------------------------
# The blocking control
# ---------------------------------------------------------------------------


class SelfDealingError(ForbiddenError):
    """403: a privileged actor may not move a linked member's money.

    Carries the sanitized refusal payload (ids and signal names only —
    never the matched emails/phones) so the API layer can persist the
    refused attempt out-of-band after the rollback."""

    def __init__(
        self,
        message: str,
        *,
        action: str,
        entity: AuditedEntity,
        entity_id: str,
        member_id: uuid.UUID,
        signals: tuple[str, ...],
        amount: Decimal | None,
    ) -> None:
        super().__init__(message)
        self.action = action
        self.entity = entity
        self.entity_id = entity_id
        self.member_id = member_id
        self.signals = signals
        self.amount = amount


async def enforce_no_self_dealing(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID,
    member_id: uuid.UUID,
    *,
    action: str,
    entity: AuditedEntity,
    entity_id: str,
    amount: Decimal | None = None,
) -> LinkageProbe:
    """Refuse (403) a privileged action whose actor is identity-linked
    to the beneficiary member; returns the probe for reuse by the
    telemetry emit (one evaluation per action). Least disclosure: the
    message carries ids and the policy, never the matched contacts."""
    probe = await probe_linkage(session, tenant_id, actor_id, member_id)
    if probe.linkage:
        raise SelfDealingError(
            f"actor {actor_id} is identity-linked to member {member_id}; "
            f"'{action}' on a linked member requires a different officer "
            "(self-dealing control)",
            action=action,
            entity=entity,
            entity_id=entity_id,
            member_id=member_id,
            signals=probe.linkage,
            amount=amount,
        )
    return probe


# ---------------------------------------------------------------------------
# The non-blocking telemetry
# ---------------------------------------------------------------------------


async def emit_anomaly_signals(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    actor_id: uuid.UUID,
    member_id: uuid.UUID,
    *,
    action: str,
    entity: AuditedEntity,
    entity_id: str,
    amount: Decimal | None = None,
    probe: LinkageProbe | None = None,
) -> tuple[str, ...]:
    """Evaluate and record anomaly signals for one privileged action.

    Returns the emitted signal tuple (empty when clean). Signal
    computation is guarded (gate 1.2 — telemetry never breaks the
    action); the audit/outbox writes are not (see module docstring).
    """
    try:
        if probe is None:
            probe = await probe_linkage(session, tenant_id, actor_id, member_id)
        signals: list[str] = list(probe.linkage)
        if is_off_hours(_now()):
            signals.append(SIGNAL_OFF_HOURS)
        if probe.cross_branch:
            signals.append(SIGNAL_CROSS_BRANCH)
    except Exception:  # noqa: BLE001 -- gate 1.2: telemetry computation must never break the action; category-only log, no PII
        logger.warning("security-signal computation failed for action=%s", action)
        return ()
    if not signals:
        return ()
    payload: dict[str, object] = {
        "action": action,
        "member_id": str(member_id),
        "signals": signals,
    }
    if amount is not None:
        payload["amount"] = str(amount)
    if probe.actor_branch_id is not None:
        payload["actor_branch_id"] = str(probe.actor_branch_id)
    if probe.member_branch_id is not None:
        payload["member_branch_id"] = str(probe.member_branch_id)
    await write_security_audit(
        session,
        tenant_id,
        actor_id,
        action="security.anomaly",
        entity=entity,
        entity_id=entity_id,
        after=payload,
    )
    await enqueue_event(
        session,
        tenant_id,
        event_type="security.anomaly",
        payload={
            "entity": entity.value,
            "entity_id": entity_id,
            "actor_id": str(actor_id),
            **payload,
        },
    )
    return tuple(signals)
