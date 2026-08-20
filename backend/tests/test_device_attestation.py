"""Device-attestation seam (#29): port, policy, adapters — falsifiable.

Pins the NON-NEGOTIABLE posture the wiring MR must preserve (the route
wiring itself is out of scope here — api/auth.py is owned by !3 and the
/member router by !7 — so the port/policy layer is tested directly):

- ENFORCE mode: an unattested request produces the SAME opaque
  response as an attested one, with NO OTP use-case call (no challenge
  row, no outbox enqueue → no SMS dispatch) and NO credential-existence
  oracle (structural: the gate takes no identifier/session at all);
- LOG-ONLY mode: the verdict is recorded (code-owned fields only —
  never the token or the challenge) and the request proceeds;
- OFF mode: the port is never even called;
- attestation is ADDITIVE to the auth `_rate_guard` (!3): a simulated
  route pipeline proves each gate fires independently — neither
  replaces the other;
- adapters: Play Integrity verdict evaluation (nonce binding, package
  identity, app recognition, device integrity, freshness) and App
  Attest assertion evaluation (challenge binding, App ID hash, counter
  monotonicity), with the credential-dependent decode/signature steps
  isolated behind their own seams — unprovisioned means a FAIL-CLOSED
  `unverifiable` verdict, never a bypass;
- the deterministic fake enforces challenge binding too (a replayed
  fake token fails), so tests/dev builds cannot drift from the real
  contract;
- boot: an unknown MEMBER_ATTESTATION_MODE refuses boot; the default
  is log-only.

All legs are pure (no DB) and run on every pipeline.
"""

import asyncio
import inspect
import logging
from collections.abc import Mapping
from dataclasses import fields
from datetime import UTC, datetime
from hashlib import sha256
from typing import Any

import pytest

from genesis.application.device_attestation import (
    ATTESTATION_MODE_ENFORCE,
    ATTESTATION_MODE_LOG_ONLY,
    ATTESTATION_MODE_OFF,
    ATTESTATION_MODES,
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
    AttestationDecision,
    AttestationVerdict,
    DeviceAttestationPort,
    assert_member_attestation_mode_valid,
    gate_member_otp_request,
)
from genesis.infrastructure.device_attestation import (
    FAKE_ATTESTED_PREFIX,
    AppAttestAssertionFacts,
    AppAttestVerifier,
    AssertionSignatureError,
    FakeDeviceAttestationAdapter,
    MalformedAttestationError,
    PlatformRoutingAttestation,
    PlayIntegrityVerifier,
    UnknownAttestationKeyError,
    default_device_attestation,
)
from genesis.settings import Settings

PACKAGE_NAME = "ke.co.genesisprestige.member"
APP_ID = "TEAMID00.ke.co.genesisprestige.member"
CHALLENGE = "srv-challenge-0001"
SECRET_TOKEN_MARKER = "tok-material-must-never-be-logged"  # noqa: S105


class RecordingAttestationPort:
    """DeviceAttestationPort double: scripted verdict, records calls."""

    def __init__(self, verdict: AttestationVerdict) -> None:
        self.verdict = verdict
        self.calls: list[dict[str, str]] = []

    async def verify_attestation(
        self, *, platform: str, token: str, challenge: str
    ) -> AttestationVerdict:
        self.calls.append({"platform": platform, "token": token, "challenge": challenge})
        return self.verdict


def _passing(platform: str = ATTESTATION_PLATFORM_ANDROID) -> AttestationVerdict:
    return AttestationVerdict(platform=platform, passed=True, reason=VERDICT_ATTESTED)


def _failing(reason: str = VERDICT_SIGNATURE_INVALID) -> AttestationVerdict:
    return AttestationVerdict(platform=ATTESTATION_PLATFORM_ANDROID, passed=False, reason=reason)


class StaticPlayIntegritySource:
    """PlayIntegrityVerdictSource double: returns a canned decoded payload."""

    def __init__(self, decoded: Mapping[str, Any] | None = None, error: Exception | None = None):
        self._decoded = decoded
        self._error = error

    async def decoded_verdict(self, token: str) -> Mapping[str, Any]:
        if self._error is not None:
            raise self._error
        assert self._decoded is not None
        return self._decoded


class StaticAppAttestValidator:
    """AppAttestAssertionValidator double: canned facts or a typed error."""

    def __init__(
        self, facts: AppAttestAssertionFacts | None = None, error: Exception | None = None
    ):
        self._facts = facts
        self._error = error

    async def validated_assertion(self, token: str) -> AppAttestAssertionFacts:
        if self._error is not None:
            raise self._error
        assert self._facts is not None
        return self._facts


def _healthy_play_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "requestDetails": {
            "nonce": CHALLENGE,
            "requestPackageName": PACKAGE_NAME,
            "timestampMillis": int(datetime.now(UTC).timestamp() * 1000),
        },
        "appIntegrity": {
            "appRecognitionVerdict": "PLAY_RECOGNIZED",
            "packageName": PACKAGE_NAME,
        },
        "deviceIntegrity": {"deviceRecognitionVerdict": ["MEETS_DEVICE_INTEGRITY"]},
    }
    for dotted, value in overrides.items():
        section, key = dotted.split("__", 1)
        payload[section] = {**payload[section], key: value}
    return payload


def _healthy_assertion_facts(**overrides: Any) -> AppAttestAssertionFacts:
    values: dict[str, Any] = {
        "rp_id_hash": sha256(APP_ID.encode()).digest(),
        "counter": 7,
        "previous_counter": 6,
        "client_data_hash": sha256(CHALLENGE.encode()).digest(),
    }
    values.update(overrides)
    return AppAttestAssertionFacts(**values)


# ---------------------------------------------------------------------------
# The seam is real: concrete adapters satisfy the port.
# ---------------------------------------------------------------------------


def test_port_contract_is_satisfied_by_the_concrete_adapters() -> None:
    routed: DeviceAttestationPort = default_device_attestation(
        android_package_name=PACKAGE_NAME, ios_app_id=APP_ID
    )
    fake: DeviceAttestationPort = FakeDeviceAttestationAdapter()
    assert routed is not None
    assert fake is not None


def test_gate_takes_no_identifier_no_session_structural_no_oracle() -> None:
    """The no-credential-oracle guarantee is STRUCTURAL: the gate cannot
    see who is asking, so its outcome can never depend on whether a
    credential exists. If someone adds an email/session/identifier
    parameter, this test fails and the oracle review re-opens."""
    params = set(inspect.signature(gate_member_otp_request).parameters)
    assert params == {"port", "mode", "platform", "token", "challenge"}


def test_decision_exposes_only_proceed_and_verdict() -> None:
    """Opaque-posture guarantee: the decision carries NOTHING a route
    could accidentally vary the response on beyond the proceed bit."""
    assert {f.name for f in fields(AttestationDecision)} == {"proceed", "verdict"}


# ---------------------------------------------------------------------------
# Policy: off / log-only / enforce.
# ---------------------------------------------------------------------------


def test_off_mode_never_calls_the_port() -> None:
    async def run() -> None:
        port = RecordingAttestationPort(_failing())
        decision = await gate_member_otp_request(
            port,
            mode=ATTESTATION_MODE_OFF,
            platform=ATTESTATION_PLATFORM_ANDROID,
            token=SECRET_TOKEN_MARKER,
            challenge=CHALLENGE,
        )
        assert decision.proceed is True
        assert decision.verdict is None
        assert port.calls == []

    asyncio.run(run())


def test_log_only_mode_records_verdict_and_proceeds(caplog: pytest.LogCaptureFixture) -> None:
    async def run() -> None:
        port = RecordingAttestationPort(_failing(VERDICT_NONCE_MISMATCH))
        with caplog.at_level(logging.INFO, logger="genesis.application.device_attestation"):
            decision = await gate_member_otp_request(
                port,
                mode=ATTESTATION_MODE_LOG_ONLY,
                platform=ATTESTATION_PLATFORM_ANDROID,
                token=SECRET_TOKEN_MARKER,
                challenge=CHALLENGE,
            )
        # A FAILING verdict still proceeds (rollout observation)...
        assert decision.proceed is True
        assert decision.verdict is not None
        assert decision.verdict.reason == VERDICT_NONCE_MISMATCH
        # ...and is RECORDED: the verdict line exists with code-owned
        # fields only.
        recorded = [r.getMessage() for r in caplog.records if "attestation verdict" in r.message]
        assert len(recorded) == 1
        assert VERDICT_NONCE_MISMATCH in recorded[0]
        # Gate 1.6: neither the token nor the challenge ever hits a log.
        assert SECRET_TOKEN_MARKER not in caplog.text
        assert CHALLENGE not in caplog.text

    asyncio.run(run())


def test_enforce_mode_denies_unattested_and_allows_attested() -> None:
    async def run() -> None:
        denied = await gate_member_otp_request(
            RecordingAttestationPort(_failing()),
            mode=ATTESTATION_MODE_ENFORCE,
            platform=ATTESTATION_PLATFORM_ANDROID,
            token=SECRET_TOKEN_MARKER,
            challenge=CHALLENGE,
        )
        allowed = await gate_member_otp_request(
            RecordingAttestationPort(_passing()),
            mode=ATTESTATION_MODE_ENFORCE,
            platform=ATTESTATION_PLATFORM_ANDROID,
            token=SECRET_TOKEN_MARKER,
            challenge=CHALLENGE,
        )
        assert denied.proceed is False
        assert allowed.proceed is True

    asyncio.run(run())


def test_missing_token_or_platform_never_reaches_the_port() -> None:
    async def run() -> None:
        for platform, token in ((None, SECRET_TOKEN_MARKER), ("android", None), (None, None)):
            port = RecordingAttestationPort(_passing())
            decision = await gate_member_otp_request(
                port,
                mode=ATTESTATION_MODE_ENFORCE,
                platform=platform,
                token=token,
                challenge=CHALLENGE,
            )
            assert decision.proceed is False
            assert decision.verdict is not None
            assert decision.verdict.reason == VERDICT_MISSING
            assert port.calls == []

    asyncio.run(run())


def test_unknown_mode_fails_closed() -> None:
    async def run() -> None:
        with pytest.raises(RuntimeError, match="fail-closed"):
            await gate_member_otp_request(
                RecordingAttestationPort(_passing()),
                mode="observe",
                platform=ATTESTATION_PLATFORM_ANDROID,
                token=SECRET_TOKEN_MARKER,
                challenge=CHALLENGE,
            )

    asyncio.run(run())


# ---------------------------------------------------------------------------
# The wiring contract, simulated end to end (the wiring MR must keep
# these invariants when it puts the gate on the real route).
# ---------------------------------------------------------------------------


class RecordingOtpService:
    """Stands in for member_auth.request_member_otp: counting it counts
    challenge rows and outbox enqueues — i.e. SMS dispatches."""

    def __init__(self) -> None:
        self.requests: list[str] = []

    async def request_member_otp(self, email: str) -> None:
        self.requests.append(email)


class SimulatedRateLimitError(Exception):
    """Stands in for the !3 RateLimitedError in the pipeline simulation."""


async def _simulated_route(
    *,
    rate_allowed: bool,
    port: DeviceAttestationPort,
    mode: str,
    platform: str | None,
    token: str | None,
    service: RecordingOtpService,
    email: str,
) -> tuple[int, dict[str, str]]:
    """The documented wiring contract, verbatim: _rate_guard FIRST
    (unchanged, !3), then the attestation gate, then — only if the
    decision proceeds — the use-case call; the response is IDENTICAL
    either way (the request_otp opaque contract)."""
    if not rate_allowed:
        raise SimulatedRateLimitError
    decision = await gate_member_otp_request(
        port, mode=mode, platform=platform, token=token, challenge=CHALLENGE
    )
    if decision.proceed:
        await service.request_member_otp(email)
    return 202, {"status": "sent"}


def test_enforce_unattested_same_opaque_response_and_no_sms() -> None:
    """THE #29 acceptance leg: in enforce mode the unattested request
    gets byte-identical status+body to the attested one, and the OTP
    use-case (challenge row, outbox enqueue → SMS) never runs."""

    async def run() -> None:
        fake = FakeDeviceAttestationAdapter()
        attested_service = RecordingOtpService()
        unattested_service = RecordingOtpService()
        attested = await _simulated_route(
            rate_allowed=True,
            port=fake,
            mode=ATTESTATION_MODE_ENFORCE,
            platform=ATTESTATION_PLATFORM_ANDROID,
            token=f"{FAKE_ATTESTED_PREFIX}{CHALLENGE}",
            service=attested_service,
            email="member@example.com",
        )
        unattested = await _simulated_route(
            rate_allowed=True,
            port=fake,
            mode=ATTESTATION_MODE_ENFORCE,
            platform=ATTESTATION_PLATFORM_ANDROID,
            token="bot-garbage",  # noqa: S106 - fake attestation material
            service=unattested_service,
            email="member@example.com",
        )
        # SAME opaque response — status and body — either way.
        assert attested == unattested == (202, {"status": "sent"})
        # SMS dispatched only for the attested request.
        assert attested_service.requests == ["member@example.com"]
        assert unattested_service.requests == []

    asyncio.run(run())


def test_no_oracle_response_identical_whether_credential_exists() -> None:
    """No credential-existence oracle: the gate ran before any lookup,
    so for the DENIED (unattested, enforce) request the response is the
    same whether or not the email has a credential — the lookup never
    happens at all."""

    async def run() -> None:
        fake = FakeDeviceAttestationAdapter()
        responses = []
        for email in ("exists@example.com", "never-registered@example.com"):
            service = RecordingOtpService()
            responses.append(
                await _simulated_route(
                    rate_allowed=True,
                    port=fake,
                    mode=ATTESTATION_MODE_ENFORCE,
                    platform=ATTESTATION_PLATFORM_IOS,
                    token="unattested",  # noqa: S106 - fake attestation material
                    service=service,
                    email=email,
                )
            )
            assert service.requests == []
        assert responses[0] == responses[1]

    asyncio.run(run())


def test_log_only_mode_request_proceeds_with_sms() -> None:
    async def run() -> None:
        service = RecordingOtpService()
        status, body = await _simulated_route(
            rate_allowed=True,
            port=FakeDeviceAttestationAdapter(),
            mode=ATTESTATION_MODE_LOG_ONLY,
            platform=ATTESTATION_PLATFORM_ANDROID,
            token="unattested",  # noqa: S106 - fake attestation material
            service=service,
            email="member@example.com",
        )
        assert (status, body) == (202, {"status": "sent"})
        assert service.requests == ["member@example.com"]

    asyncio.run(run())


def test_attestation_is_additive_to_rate_guard_both_gates_fire() -> None:
    """Attestation ADDS to _rate_guard (!3), it never replaces it. Each
    gate fires with the other one green — and the rate guard keeps its
    !3 semantics (429 posture) even for a fully attested caller."""

    async def run() -> None:
        fake = FakeDeviceAttestationAdapter()
        good_token = f"{FAKE_ATTESTED_PREFIX}{CHALLENGE}"
        # Leg 1: attested but rate-limited → the rate guard still fires.
        service = RecordingOtpService()
        with pytest.raises(SimulatedRateLimitError):
            await _simulated_route(
                rate_allowed=False,
                port=fake,
                mode=ATTESTATION_MODE_ENFORCE,
                platform=ATTESTATION_PLATFORM_ANDROID,
                token=good_token,
                service=service,
                email="member@example.com",
            )
        assert service.requests == []
        # Leg 2: under the rate limit but unattested → the attestation
        # gate still fires (opaque, no dispatch).
        service = RecordingOtpService()
        response = await _simulated_route(
            rate_allowed=True,
            port=fake,
            mode=ATTESTATION_MODE_ENFORCE,
            platform=ATTESTATION_PLATFORM_ANDROID,
            token="unattested",  # noqa: S106 - fake attestation material
            service=service,
            email="member@example.com",
        )
        assert response == (202, {"status": "sent"})
        assert service.requests == []
        # Leg 3: both green → exactly one dispatch.
        service = RecordingOtpService()
        await _simulated_route(
            rate_allowed=True,
            port=fake,
            mode=ATTESTATION_MODE_ENFORCE,
            platform=ATTESTATION_PLATFORM_ANDROID,
            token=good_token,
            service=service,
            email="member@example.com",
        )
        assert service.requests == ["member@example.com"]

    asyncio.run(run())


# ---------------------------------------------------------------------------
# Deterministic fake: honest to the real contract.
# ---------------------------------------------------------------------------


def test_fake_adapter_is_deterministic_and_challenge_bound() -> None:
    async def run() -> None:
        fake = FakeDeviceAttestationAdapter()
        for platform in (ATTESTATION_PLATFORM_ANDROID, ATTESTATION_PLATFORM_IOS):
            good = await fake.verify_attestation(
                platform=platform, token=f"{FAKE_ATTESTED_PREFIX}{CHALLENGE}", challenge=CHALLENGE
            )
            assert (good.passed, good.reason) == (True, VERDICT_ATTESTED)
        # Challenge binding holds even in the fake: a replayed fake
        # token (minted for another challenge) FAILS.
        replayed = await fake.verify_attestation(
            platform=ATTESTATION_PLATFORM_ANDROID,
            token=f"{FAKE_ATTESTED_PREFIX}stale-challenge",
            challenge=CHALLENGE,
        )
        assert (replayed.passed, replayed.reason) == (False, VERDICT_NONCE_MISMATCH)
        garbage = await fake.verify_attestation(
            platform=ATTESTATION_PLATFORM_ANDROID,
            token="garbage",  # noqa: S106 - fake attestation material
            challenge=CHALLENGE,
        )
        assert (garbage.passed, garbage.reason) == (False, VERDICT_SIGNATURE_INVALID)
        empty = await fake.verify_attestation(
            platform=ATTESTATION_PLATFORM_IOS, token="", challenge=CHALLENGE
        )
        assert (empty.passed, empty.reason) == (False, VERDICT_MISSING)
        alien = await fake.verify_attestation(
            platform="web", token=f"{FAKE_ATTESTED_PREFIX}{CHALLENGE}", challenge=CHALLENGE
        )
        assert (alien.passed, alien.reason) == (False, VERDICT_UNSUPPORTED_PLATFORM)

    asyncio.run(run())


# ---------------------------------------------------------------------------
# Play Integrity verdict evaluation (Android).
# ---------------------------------------------------------------------------


def _play_verifier(source: StaticPlayIntegritySource) -> PlayIntegrityVerifier:
    return PlayIntegrityVerifier(source, package_name=PACKAGE_NAME)


def _play_verdict(source: StaticPlayIntegritySource) -> AttestationVerdict:
    return asyncio.run(
        _play_verifier(source).verify_attestation(
            platform=ATTESTATION_PLATFORM_ANDROID,
            token=SECRET_TOKEN_MARKER,
            challenge=CHALLENGE,
        )
    )


def test_play_integrity_healthy_verdict_passes() -> None:
    verdict = _play_verdict(StaticPlayIntegritySource(_healthy_play_payload()))
    assert (verdict.passed, verdict.reason) == (True, VERDICT_ATTESTED)


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"requestDetails__nonce": "other-challenge"}, VERDICT_NONCE_MISMATCH),
        ({"requestDetails__requestPackageName": "com.evil.app"}, VERDICT_PACKAGE_MISMATCH),
        ({"appIntegrity__packageName": "com.evil.app"}, VERDICT_PACKAGE_MISMATCH),
        ({"appIntegrity__appRecognitionVerdict": "UNRECOGNIZED_VERSION"}, VERDICT_APP_UNRECOGNIZED),
        ({"deviceIntegrity__deviceRecognitionVerdict": []}, VERDICT_DEVICE_INTEGRITY),
        ({"requestDetails__timestampMillis": 1_000}, VERDICT_STALE),
        ({"requestDetails__timestampMillis": "not-a-timestamp"}, VERDICT_MALFORMED),
    ],
)
def test_play_integrity_each_check_point_fails_closed(
    overrides: dict[str, Any], reason: str
) -> None:
    verdict = _play_verdict(StaticPlayIntegritySource(_healthy_play_payload(**overrides)))
    assert (verdict.passed, verdict.reason) == (False, reason)


def test_play_integrity_missing_sections_are_malformed() -> None:
    verdict = _play_verdict(StaticPlayIntegritySource({"requestDetails": {"nonce": CHALLENGE}}))
    assert (verdict.passed, verdict.reason) == (False, VERDICT_MALFORMED)


def test_play_integrity_unprovisioned_is_fail_closed_unverifiable(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The shipped default (no Google credentials yet — a HUMAN
    provisioning step) yields `unverifiable`: enforce mode denies, and
    the token never hits the log."""
    port = default_device_attestation(android_package_name=PACKAGE_NAME, ios_app_id=APP_ID)
    with caplog.at_level(logging.WARNING, logger="genesis.infrastructure.device_attestation"):
        verdict = asyncio.run(
            port.verify_attestation(
                platform=ATTESTATION_PLATFORM_ANDROID,
                token=SECRET_TOKEN_MARKER,
                challenge=CHALLENGE,
            )
        )
    assert (verdict.passed, verdict.reason) == (False, VERDICT_UNVERIFIABLE)
    assert SECRET_TOKEN_MARKER not in caplog.text


def test_play_integrity_malformed_token_is_malformed() -> None:
    verdict = _play_verdict(StaticPlayIntegritySource(error=MalformedAttestationError()))
    assert (verdict.passed, verdict.reason) == (False, VERDICT_MALFORMED)


# ---------------------------------------------------------------------------
# App Attest assertion evaluation (iOS).
# ---------------------------------------------------------------------------


def _app_attest_verdict(validator: StaticAppAttestValidator) -> AttestationVerdict:
    verifier = AppAttestVerifier(validator, app_id=APP_ID)
    return asyncio.run(
        verifier.verify_attestation(
            platform=ATTESTATION_PLATFORM_IOS, token=SECRET_TOKEN_MARKER, challenge=CHALLENGE
        )
    )


def test_app_attest_healthy_assertion_passes() -> None:
    verdict = _app_attest_verdict(StaticAppAttestValidator(_healthy_assertion_facts()))
    assert (verdict.passed, verdict.reason) == (True, VERDICT_ATTESTED)


@pytest.mark.parametrize(
    ("facts", "reason"),
    [
        (
            _healthy_assertion_facts(client_data_hash=sha256(b"other-challenge").digest()),
            VERDICT_NONCE_MISMATCH,
        ),
        (
            _healthy_assertion_facts(rp_id_hash=sha256(b"OTHER.bundle.id").digest()),
            VERDICT_APP_ID_MISMATCH,
        ),
        (_healthy_assertion_facts(counter=6, previous_counter=6), VERDICT_COUNTER_REGRESSION),
        (_healthy_assertion_facts(counter=5, previous_counter=6), VERDICT_COUNTER_REGRESSION),
    ],
)
def test_app_attest_each_check_point_fails_closed(
    facts: AppAttestAssertionFacts, reason: str
) -> None:
    verdict = _app_attest_verdict(StaticAppAttestValidator(facts))
    assert (verdict.passed, verdict.reason) == (False, reason)


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (UnknownAttestationKeyError(), VERDICT_KEY_UNKNOWN),
        (AssertionSignatureError(), VERDICT_SIGNATURE_INVALID),
        (MalformedAttestationError(), VERDICT_MALFORMED),
    ],
)
def test_app_attest_validator_errors_map_to_verdicts(error: Exception, reason: str) -> None:
    verdict = _app_attest_verdict(StaticAppAttestValidator(error=error))
    assert (verdict.passed, verdict.reason) == (False, reason)


def test_app_attest_unprovisioned_is_fail_closed_unverifiable() -> None:
    port = default_device_attestation(android_package_name=PACKAGE_NAME, ios_app_id=APP_ID)
    verdict = asyncio.run(
        port.verify_attestation(
            platform=ATTESTATION_PLATFORM_IOS, token=SECRET_TOKEN_MARKER, challenge=CHALLENGE
        )
    )
    assert (verdict.passed, verdict.reason) == (False, VERDICT_UNVERIFIABLE)


def test_platform_router_rejects_unknown_platform() -> None:
    port = PlatformRoutingAttestation({})
    verdict = asyncio.run(
        port.verify_attestation(platform="web", token=SECRET_TOKEN_MARKER, challenge=CHALLENGE)
    )
    assert (verdict.passed, verdict.reason) == (False, VERDICT_UNSUPPORTED_PLATFORM)


# ---------------------------------------------------------------------------
# Settings: env-only enforcement mode, boot-validated.
# ---------------------------------------------------------------------------


def test_default_mode_is_log_only() -> None:
    assert Settings.model_fields["member_attestation_mode"].default == ATTESTATION_MODE_LOG_ONLY
    assert ATTESTATION_MODES == ("off", "log-only", "enforce")


def test_boot_guard_accepts_every_known_mode_and_refuses_the_rest() -> None:
    for mode in ATTESTATION_MODES:
        assert_member_attestation_mode_valid(Settings(member_attestation_mode=mode))
    for bad in ("", "on", "enforced", "LOG-ONLY", "log_only"):
        with pytest.raises(RuntimeError, match="MEMBER_ATTESTATION_MODE"):
            assert_member_attestation_mode_valid(Settings(member_attestation_mode=bad))
