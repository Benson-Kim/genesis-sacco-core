"""Device-attestation adapters behind the application port (#29).

Implements `genesis.application.device_attestation.DeviceAttestationPort`
for the member OTP request gate. Layout mirrors
`infrastructure.otp_delivery` (the seam pattern):

* `PlayIntegrityVerdictSource` / `AppAttestAssertionValidator` — the
  EXTERNAL-CREDENTIAL-DEPENDENT steps, isolated behind their own
  protocols because provisioning is a HUMAN step (a Google Cloud
  service account with the Play Integrity API enabled; the Apple App
  Attest root CA + a registered-key store). Everything that needs a
  secret or a network call lives behind these two seams and NOWHERE
  else — the verdict-evaluation logic above them is deterministic and
  fully tested today, before any credential exists.
* `UnprovisionedPlayIntegrityVerdictSource` /
  `UnprovisionedAppAttestAssertionValidator` — the shipped defaults:
  raise `AttestationUnavailableError`, which the verifiers map to a
  FAIL-CLOSED `unverifiable` verdict. In enforce mode an unverifiable
  request is a denied request; provisioning credentials is an
  operations task, never a security bypass.
* `PlayIntegrityVerifier` — Android: evaluates a DECODED Play
  Integrity verdict payload (the source owns the decode: Google-side
  `decodeIntegrityToken` or local JWE/JWS unwrap): nonce binding to
  the server challenge, requesting/attested package-name match,
  `appRecognitionVerdict == PLAY_RECOGNIZED`,
  `MEETS_DEVICE_INTEGRITY` in the device verdict, and token freshness.
* `AppAttestVerifier` — iOS: evaluates VALIDATED assertion facts (the
  validator owns key-registry lookup + signature verification, and at
  key registration the certificate chain to Apple's App Attest root):
  challenge binding via the client-data hash, App ID (RP ID) hash
  match, and counter monotonicity (replay defense).
* `PlatformRoutingAttestation` — the port implementation: routes by
  platform; an unknown platform is an attestation FAILURE
  (`unsupported-platform`), never a bypass.
* `FakeDeviceAttestationAdapter` — DETERMINISTIC fake for tests/dev:
  `fake-attested:<challenge>` passes (challenge binding included even
  in the fake — a replayed fake token fails), everything else fails
  with a stable reason. No I/O, no randomness.

Request handlers never import this module (import-linter contract,
extended in pyproject.toml alongside the otp_delivery entry): the
wiring MR injects the port from the composition root
(`default_device_attestation`). NEVER log or persist tokens,
assertions, or challenges — verdict reason codes only (gate 1.6).
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from genesis.application.device_attestation import (
    ATTESTATION_PLATFORM_ANDROID,
    ATTESTATION_PLATFORM_IOS,
    VERDICT_APP_ID_MISMATCH,
    VERDICT_APP_UNRECOGNIZED,
    VERDICT_ATTESTED,
    VERDICT_COUNTER_REGRESSION,
    VERDICT_DEVICE_INTEGRITY,
    VERDICT_KEY_UNKNOWN,
    VERDICT_MALFORMED,
    VERDICT_MISSING,
    VERDICT_NONCE_MISMATCH,
    VERDICT_PACKAGE_MISMATCH,
    VERDICT_SIGNATURE_INVALID,
    VERDICT_STALE,
    VERDICT_UNSUPPORTED_PLATFORM,
    VERDICT_UNVERIFIABLE,
    AttestationVerdict,
)

logger = logging.getLogger("genesis.infrastructure.device_attestation")

#: Maximum accepted age of attestation material. Tokens are minted per
#: challenge, and the OTP challenge itself lives 5 minutes — anything
#: older is a replay candidate and fails as STALE.
MAX_TOKEN_AGE_SECONDS = 600

#: Play Integrity verdict values (the Google-documented constants the
#: evaluation pins). Only these exact values pass.
PLAY_RECOGNIZED = "PLAY_RECOGNIZED"
MEETS_DEVICE_INTEGRITY = "MEETS_DEVICE_INTEGRITY"

#: Deterministic fake token prefix (tests/dev builds only).
FAKE_ATTESTED_PREFIX = "fake-attested:"


class AttestationUnavailableError(Exception):
    """The verification dependency cannot run (credentials not
    provisioned, upstream outage). Mapped to a fail-closed
    `unverifiable` verdict by the verifiers — never a bypass."""


class UnknownAttestationKeyError(Exception):
    """App Attest: the presented key id has no registered public key."""


class AssertionSignatureError(Exception):
    """App Attest: the assertion signature does not verify against the
    registered public key."""


class MalformedAttestationError(Exception):
    """The attestation material cannot be parsed at all."""


# ---------------------------------------------------------------------------
# Play Integrity (Android)
# ---------------------------------------------------------------------------


class PlayIntegrityVerdictSource(Protocol):
    """The Google-credential-dependent step, isolated (human provisioning).

    Given the opaque integrity token, return the DECODED verdict payload
    (the `tokenPayloadExternal` shape: requestDetails / appIntegrity /
    deviceIntegrity). A real implementation calls
    `playintegrity.googleapis.com decodeIntegrityToken` with a service
    account, or unwraps the JWE/JWS locally with managed decryption
    keys — either way the decode, and ONLY the decode, needs Google
    credentials. Raises `AttestationUnavailableError` when it cannot
    run and `MalformedAttestationError` when the token is not even
    parseable.
    """

    async def decoded_verdict(self, token: str) -> Mapping[str, Any]:
        """Decode one integrity token into its verdict payload."""
        ...


class UnprovisionedPlayIntegrityVerdictSource:
    """Shipped default until the Google Cloud service account exists.

    Raising here yields a fail-closed `unverifiable` verdict: enforce
    mode DENIES until a human provisions credentials and swaps in the
    real source at the composition root.
    """

    async def decoded_verdict(self, token: str) -> Mapping[str, Any]:
        raise AttestationUnavailableError(
            "Play Integrity verification is not provisioned (no Google "
            "Cloud service account configured)"
        )


class PlayIntegrityVerifier:
    """Evaluate a decoded Play Integrity verdict for one challenge.

    The evaluation order is deliberate: nonce binding FIRST (an unbound
    token is worthless no matter how healthy the device looks), then
    package identity, app recognition, device integrity, freshness.
    Every failure is a stable code-owned reason — decoded payload
    values never leak into logs or errors.
    """

    platform = ATTESTATION_PLATFORM_ANDROID

    def __init__(self, source: PlayIntegrityVerdictSource, *, package_name: str) -> None:
        self._source = source
        self._package_name = package_name

    def _fail(self, reason: str) -> AttestationVerdict:
        return AttestationVerdict(platform=self.platform, passed=False, reason=reason)

    async def verify_attestation(
        self, *, platform: str, token: str, challenge: str
    ) -> AttestationVerdict:
        if not token:
            return self._fail(VERDICT_MISSING)
        try:
            decoded = await self._source.decoded_verdict(token)
        except AttestationUnavailableError:
            logger.warning("play integrity verdict unavailable — failing closed (unverifiable)")
            return self._fail(VERDICT_UNVERIFIABLE)
        except MalformedAttestationError:
            return self._fail(VERDICT_MALFORMED)
        return self._evaluate(decoded, challenge)

    def _evaluate(self, decoded: Mapping[str, Any], challenge: str) -> AttestationVerdict:
        request_details = decoded.get("requestDetails")
        app_integrity = decoded.get("appIntegrity")
        device_integrity = decoded.get("deviceIntegrity")
        if not (
            isinstance(request_details, Mapping)
            and isinstance(app_integrity, Mapping)
            and isinstance(device_integrity, Mapping)
        ):
            return self._fail(VERDICT_MALFORMED)
        # 1. Nonce binding: the token must have been minted for THIS
        # server-issued challenge, or a captured token replays forever.
        if request_details.get("nonce") != challenge:
            return self._fail(VERDICT_NONCE_MISMATCH)
        # 2. Package identity: both the requesting package and the
        # attested package must be ours.
        if (
            request_details.get("requestPackageName") != self._package_name
            or app_integrity.get("packageName") != self._package_name
        ):
            return self._fail(VERDICT_PACKAGE_MISMATCH)
        # 3. App recognition: the exact Play-recognized build.
        if app_integrity.get("appRecognitionVerdict") != PLAY_RECOGNIZED:
            return self._fail(VERDICT_APP_UNRECOGNIZED)
        # 4. Device integrity: the baseline verdict must be present.
        verdicts = device_integrity.get("deviceRecognitionVerdict")
        if not (isinstance(verdicts, list) and MEETS_DEVICE_INTEGRITY in verdicts):
            return self._fail(VERDICT_DEVICE_INTEGRITY)
        # 5. Freshness: a token older than MAX_TOKEN_AGE_SECONDS is a
        # replay candidate.
        timestamp_ms = request_details.get("timestampMillis")
        if not isinstance(timestamp_ms, int):
            return self._fail(VERDICT_MALFORMED)
        age = datetime.now(UTC) - datetime.fromtimestamp(timestamp_ms / 1000, tz=UTC)
        if age.total_seconds() > MAX_TOKEN_AGE_SECONDS or age.total_seconds() < 0:
            return self._fail(VERDICT_STALE)
        return AttestationVerdict(platform=self.platform, passed=True, reason=VERDICT_ATTESTED)


# ---------------------------------------------------------------------------
# App Attest (iOS)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AppAttestAssertionFacts:
    """What the credential-dependent validation step yields.

    Producing these facts REQUIRES the registered key material: the
    validator has already verified the assertion signature over
    `authenticatorData || clientDataHash` with the public key registered
    for the presented key id (and, at registration time, the key's
    certificate chain up to Apple's App Attest root CA). The verifier
    above only evaluates the facts.
    """

    #: SHA-256 of the App ID (team id + bundle id) the assertion was
    #: produced for — the authenticator data's RP ID hash.
    rp_id_hash: bytes
    #: The assertion counter from the authenticator data.
    counter: int
    #: The counter recorded for this key at its previous use (0 for a
    #: freshly registered key).
    previous_counter: int
    #: SHA-256 of the client data the signature covers — must equal the
    #: hash of the server challenge (challenge binding).
    client_data_hash: bytes


class AppAttestAssertionValidator(Protocol):
    """The Apple-credential-dependent step, isolated (human provisioning).

    A real implementation parses the CBOR assertion envelope, looks up
    the registered public key for the key id (registration itself
    verifies the attestation object's certificate chain to Apple's App
    Attest root CA), verifies the assertion signature, and returns the
    facts. Raises `AttestationUnavailableError` when the key store /
    root CA is not provisioned, `UnknownAttestationKeyError` for an
    unregistered key id, `AssertionSignatureError` for a bad signature,
    and `MalformedAttestationError` for unparseable material.
    """

    async def validated_assertion(self, token: str) -> AppAttestAssertionFacts:
        """Validate one assertion envelope and return its facts."""
        ...


class UnprovisionedAppAttestAssertionValidator:
    """Shipped default until the Apple key store/root CA is provisioned.

    Same fail-closed posture as the Play Integrity twin: `unverifiable`
    denies in enforce mode until a human completes provisioning.
    """

    async def validated_assertion(self, token: str) -> AppAttestAssertionFacts:
        raise AttestationUnavailableError(
            "App Attest verification is not provisioned (no registered-key "
            "store / Apple App Attest root CA configured)"
        )


class AppAttestVerifier:
    """Evaluate validated App Attest assertion facts for one challenge."""

    platform = ATTESTATION_PLATFORM_IOS

    def __init__(self, validator: AppAttestAssertionValidator, *, app_id: str) -> None:
        self._validator = validator
        self._app_id_hash = hashlib.sha256(app_id.encode()).digest()

    def _fail(self, reason: str) -> AttestationVerdict:
        return AttestationVerdict(platform=self.platform, passed=False, reason=reason)

    async def verify_attestation(
        self, *, platform: str, token: str, challenge: str
    ) -> AttestationVerdict:
        if not token:
            return self._fail(VERDICT_MISSING)
        try:
            facts = await self._validator.validated_assertion(token)
        except AttestationUnavailableError:
            logger.warning("app attest validation unavailable — failing closed (unverifiable)")
            return self._fail(VERDICT_UNVERIFIABLE)
        except UnknownAttestationKeyError:
            return self._fail(VERDICT_KEY_UNKNOWN)
        except AssertionSignatureError:
            return self._fail(VERDICT_SIGNATURE_INVALID)
        except MalformedAttestationError:
            return self._fail(VERDICT_MALFORMED)
        # 1. Challenge binding: the signed client data must hash the
        # SERVER-issued challenge, or a captured assertion replays.
        if facts.client_data_hash != hashlib.sha256(challenge.encode()).digest():
            return self._fail(VERDICT_NONCE_MISMATCH)
        # 2. App identity: the assertion must come from OUR App ID.
        if facts.rp_id_hash != self._app_id_hash:
            return self._fail(VERDICT_APP_ID_MISMATCH)
        # 3. Counter monotonicity: a non-increasing counter marks a
        # cloned key or a replayed assertion.
        if facts.counter <= facts.previous_counter:
            return self._fail(VERDICT_COUNTER_REGRESSION)
        return AttestationVerdict(platform=self.platform, passed=True, reason=VERDICT_ATTESTED)


# ---------------------------------------------------------------------------
# Port implementation + composition root
# ---------------------------------------------------------------------------


class _PlatformVerifier(Protocol):
    """Internal: what PlatformRoutingAttestation routes to."""

    async def verify_attestation(
        self, *, platform: str, token: str, challenge: str
    ) -> AttestationVerdict:
        """Verify one attestation token for one platform."""
        ...


class PlatformRoutingAttestation:
    """Concrete DeviceAttestationPort: routes by client platform.

    An unknown platform is an attestation FAILURE
    (`unsupported-platform`) — never a bypass and never an exception:
    the platform string is caller-controlled, so raising on it would
    hand callers a 500 oracle.
    """

    def __init__(self, verifiers: Mapping[str, _PlatformVerifier]) -> None:
        self._verifiers = dict(verifiers)

    async def verify_attestation(
        self, *, platform: str, token: str, challenge: str
    ) -> AttestationVerdict:
        verifier = self._verifiers.get(platform)
        if verifier is None:
            return AttestationVerdict(
                platform=platform, passed=False, reason=VERDICT_UNSUPPORTED_PLATFORM
            )
        return await verifier.verify_attestation(
            platform=platform, token=token, challenge=challenge
        )


class FakeDeviceAttestationAdapter:
    """DETERMINISTIC DeviceAttestationPort for tests and dev builds.

    Contract: a token of exactly `fake-attested:<challenge>` passes for
    the two real platforms — the challenge binding is enforced even in
    the fake, so a replayed fake token FAILS (nonce-mismatch), keeping
    test behavior honest to the real adapters. Everything else fails
    with a stable reason. No I/O, no randomness, no credentials.
    """

    async def verify_attestation(
        self, *, platform: str, token: str, challenge: str
    ) -> AttestationVerdict:
        if platform not in (ATTESTATION_PLATFORM_ANDROID, ATTESTATION_PLATFORM_IOS):
            return AttestationVerdict(
                platform=platform, passed=False, reason=VERDICT_UNSUPPORTED_PLATFORM
            )
        if not token:
            return AttestationVerdict(platform=platform, passed=False, reason=VERDICT_MISSING)
        if not token.startswith(FAKE_ATTESTED_PREFIX):
            return AttestationVerdict(
                platform=platform, passed=False, reason=VERDICT_SIGNATURE_INVALID
            )
        if token != f"{FAKE_ATTESTED_PREFIX}{challenge}":
            return AttestationVerdict(
                platform=platform, passed=False, reason=VERDICT_NONCE_MISMATCH
            )
        return AttestationVerdict(platform=platform, passed=True, reason=VERDICT_ATTESTED)


def default_device_attestation(
    *, android_package_name: str, ios_app_id: str
) -> PlatformRoutingAttestation:
    """Composition root for device attestation.

    The wiring MR injects the result of this factory into the member
    OTP request gate (handlers cannot import this module — the
    import-linter contract). Until a human provisions the external
    credentials (Google Cloud service account for Play Integrity; the
    Apple registered-key store for App Attest), both platforms verify
    through the Unprovisioned sources and every verdict is a
    fail-closed `unverifiable` — swap in the real
    PlayIntegrityVerdictSource / AppAttestAssertionValidator HERE and
    nothing above this module changes (the seam is the point).
    """
    return PlatformRoutingAttestation(
        {
            ATTESTATION_PLATFORM_ANDROID: PlayIntegrityVerifier(
                UnprovisionedPlayIntegrityVerdictSource(),
                package_name=android_package_name,
            ),
            ATTESTATION_PLATFORM_IOS: AppAttestVerifier(
                UnprovisionedAppAttestAssertionValidator(),
                app_id=ios_app_id,
            ),
        }
    )
