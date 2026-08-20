"""Device-attestation port: the seam gating the member OTP request (#29).

The member mobile app design brief (docs/technical/member-mobile-app.md
§2, landed via the !6 docs review) declares device attestation
NON-NEGOTIABLE: Play Integrity (Android) / App Attest (iOS) verified
SERVER-SIDE as a precondition for `POST /member/auth/otp/request`.
Threat model: automated/bot traffic exceeding human traffic — without
this gate the route is an SMS-cost and harassment vector the moment a
real (paid) SMS gateway lands (#18). A client-asserted "I am attested"
flag is a rejected design; only a verified verdict counts.

This module is the APPLICATION side of the hexagonal seam, mirroring
`genesis.application.otp_delivery.OtpDeliveryPort` exactly: pure
declarations plus the enforcement POLICY, no I/O, no framework imports.
Concrete verifiers (Play Integrity verdict decoding, App Attest
assertion validation, and the deterministic fake for tests/dev) live in
`genesis.infrastructure.device_attestation`; request handlers can never
import them directly (import-linter contract — the wiring injects the
port from the composition root).

WIRING CONTRACT (for the follow-up wiring MR — this MR ships the seam
only, because `api/auth.py` is owned by !3 and the /member router by
!7):

* The gate runs BEFORE any credential lookup: `gate_member_otp_request`
  deliberately takes NO session, NO email/identifier — structurally it
  cannot become a credential-existence oracle, and its outcome can
  never depend on whether a credential exists.
* In ENFORCE mode a denied request MUST produce the SAME opaque
  response as an allowed one (the request_otp contract: 202
  `{"status": "sent"}`), with NO challenge row written and NO outbox
  enqueue — hence NO SMS dispatch. `AttestationDecision.proceed` is the
  ONLY signal: False means "skip the use-case call, return the normal
  opaque body".
* Attestation is ADDITIVE to the `_rate_guard` on the route (see !3),
  never a replacement: the wiring MR must keep BOTH dependencies and
  assert each gate fires independently (rate-limited requests are
  refused even when attested; unattested requests dispatch no SMS even
  when under the rate limit).

Enforcement mode (`MEMBER_ATTESTATION_MODE`, settings.py) rolls out
`off` → `log-only` → `enforce` so early app builds are not bricked;
`enforce` before the first PAID SMS is the deadline (#18). The mode is
boot-validated by `assert_member_attestation_mode_valid` below (the
`assert_cursor_signing_key_configured` posture) — an unknown mode
refuses boot, never a silent fallback.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol

from genesis.settings import Settings, get_settings

logger = logging.getLogger("genesis.application.device_attestation")

#: Client platforms the member app attests from. Anything else is an
#: attestation FAILURE (VERDICT_UNSUPPORTED_PLATFORM), never a bypass.
ATTESTATION_PLATFORM_ANDROID = "android"
ATTESTATION_PLATFORM_IOS = "ios"

#: Enforcement modes, in rollout order. `off` skips verification
#: entirely (pre-app builds only); `log-only` verifies and RECORDS the
#: verdict but always lets the request proceed (rollout observation);
#: `enforce` refuses unattested requests — opaquely (see the wiring
#: contract in the module docstring).
ATTESTATION_MODE_OFF = "off"
ATTESTATION_MODE_LOG_ONLY = "log-only"
ATTESTATION_MODE_ENFORCE = "enforce"
ATTESTATION_MODES = (
    ATTESTATION_MODE_OFF,
    ATTESTATION_MODE_LOG_ONLY,
    ATTESTATION_MODE_ENFORCE,
)

#: Verdict reason codes — CODE-OWNED constants only. A reason NEVER
#: carries token material, challenge values, or any caller-supplied
#: byte (gate 1.6: these codes end up in logs).
VERDICT_ATTESTED = "attested"
VERDICT_MISSING = "missing"
VERDICT_UNSUPPORTED_PLATFORM = "unsupported-platform"
VERDICT_MALFORMED = "malformed"
VERDICT_NONCE_MISMATCH = "nonce-mismatch"
VERDICT_PACKAGE_MISMATCH = "package-mismatch"
VERDICT_APP_UNRECOGNIZED = "app-unrecognized"
VERDICT_DEVICE_INTEGRITY = "device-integrity"
VERDICT_STALE = "stale"
VERDICT_KEY_UNKNOWN = "key-unknown"
VERDICT_SIGNATURE_INVALID = "signature-invalid"
VERDICT_COUNTER_REGRESSION = "counter-regression"
VERDICT_APP_ID_MISMATCH = "app-id-mismatch"
#: The verification dependency itself is unavailable (credentials not
#: yet provisioned, upstream outage). FAIL-CLOSED: in enforce mode an
#: unverifiable request is a DENIED request — availability of the
#: verifier is an operations problem, never a bypass.
VERDICT_UNVERIFIABLE = "unverifiable"


@dataclass(frozen=True)
class AttestationVerdict:
    """One verified attestation outcome.

    `reason` is always one of the VERDICT_* constants above — verifiers
    MUST NOT smuggle token contents or challenge values into it.
    """

    platform: str
    passed: bool
    reason: str


class DeviceAttestationPort(Protocol):
    """Verification seam for device-attestation material (#29).

    `token` is the OPAQUE client-supplied attestation material (a Play
    Integrity token on Android, an App Attest key id + assertion
    envelope on iOS); `challenge` is the SERVER-issued nonce the token
    must be bound to — implementations MUST check the binding, or a
    captured token replays forever.

    Implementations return a verdict for every EXPECTED failure
    (missing/malformed/unbound/unrecognized/unverifiable material) and
    raise only on programmer error. They MUST NEVER log or persist the
    token or the challenge (gate 1.6) — verdict reason codes only.
    """

    async def verify_attestation(
        self, *, platform: str, token: str, challenge: str
    ) -> AttestationVerdict:
        """Verify one attestation token against one server challenge."""
        ...


@dataclass(frozen=True)
class AttestationDecision:
    """The policy outcome the route wiring acts on.

    `proceed` is the ONLY control signal: False (possible ONLY in
    enforce mode) means "do not call the OTP use-case — no challenge
    row, no outbox enqueue, no SMS — and return the SAME opaque 202
    body an allowed request gets". The decision deliberately exposes
    nothing else the response could vary on (opaque-posture guarantee).
    `verdict` is the recorded outcome (None only in `off` mode).
    """

    proceed: bool
    verdict: AttestationVerdict | None


async def gate_member_otp_request(
    port: DeviceAttestationPort,
    *,
    mode: str,
    platform: str | None,
    token: str | None,
    challenge: str,
) -> AttestationDecision:
    """Apply the enforcement-mode policy to one member OTP request.

    Runs BEFORE any credential lookup and takes no identifier/session on
    purpose — the no-oracle guarantee is structural (see the module
    docstring's wiring contract). An unknown `mode` raises (fail-closed;
    `assert_member_attestation_mode_valid` makes that unreachable after
    boot). Missing platform/token never reaches the port: it is a
    MISSING verdict outright.

    The verdict is recorded via a structured log line (code-owned
    fields only — platform, reason, mode; NEVER the token or the
    challenge). Verdict persistence beyond logs is deliberately out of
    scope here: if the wiring MR wants durable verdict analytics it
    rides the EXISTING outbox surface (`application.outbox.enqueue_event`)
    in the request transaction — no new table, no migration.
    """
    if mode not in ATTESTATION_MODES:
        msg = f"unknown MEMBER_ATTESTATION_MODE '{mode}' — refusing to guess (fail-closed)"
        raise RuntimeError(msg)
    if mode == ATTESTATION_MODE_OFF:
        return AttestationDecision(proceed=True, verdict=None)
    if not platform or not token:
        verdict = AttestationVerdict(
            platform=platform or "", passed=False, reason=VERDICT_MISSING
        )
    else:
        verdict = await port.verify_attestation(
            platform=platform, token=token, challenge=challenge
        )
    logger.info(
        "member otp attestation verdict: platform=%s passed=%s reason=%s mode=%s",
        verdict.platform or "none",
        verdict.passed,
        verdict.reason,
        mode,
    )
    if mode == ATTESTATION_MODE_LOG_ONLY:
        return AttestationDecision(proceed=True, verdict=verdict)
    return AttestationDecision(proceed=verdict.passed, verdict=verdict)


def assert_member_attestation_mode_valid(settings: Settings | None = None) -> None:
    """Fail-closed BOOT guard: an unknown enforcement mode refuses boot.

    A typo'd MEMBER_ATTESTATION_MODE must never silently degrade to
    "off" (or crash on the first member OTP request) — the deployment
    fails loudly at startup instead, exactly like
    `assert_cursor_signing_key_configured` and
    `assert_dev_otp_display_dev_only`. Called by
    `genesis.api.app.create_app` before any router is wired.
    """
    resolved = settings if settings is not None else get_settings()
    if resolved.member_attestation_mode not in ATTESTATION_MODES:
        raise RuntimeError(
            "MEMBER_ATTESTATION_MODE is "
            f"'{resolved.member_attestation_mode}' — must be one of "
            f"{', '.join(ATTESTATION_MODES)}; refusing to boot with an "
            "unknown attestation enforcement mode"
        )
