"""#35 item 11 — DEV-ONLY OTP display behind the fail-closed flag.

REMOVAL NOTE: this whole affordance (dev_otp_display + the api/auth.py
consumer + this suite) must be removed before staging (tracked on #35).

Falsifiable BOTH WAYS (§4):
- flag OFF (the default — fail-closed): the /auth/otp/request response
  carries NO dev_otp key, even though a challenge was issued;
- flag ON: dev_otp is present, six digits, and actually VERIFIES (the
  displayed code is the real challenge, not an echo);
- the code NEVER appears in log output on either path (gate 1.6);
- an unknown email with the flag ON still returns the bare
  {"status": "sent"} — user existence is not revealed beyond the
  dev-mode display itself.
"""

import asyncio
import logging
import os
import re
import uuid

import pytest

from db_helpers import api_client, seed_user, unique_email
from genesis.settings import get_settings

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="requires a migrated database"
)


@pytest.fixture()
def _dev_otp_on():
    """Enable the flag via the environment, exactly as an operator would."""
    os.environ["DEV_OTP_DISPLAY"] = "1"
    get_settings.cache_clear()
    yield
    os.environ.pop("DEV_OTP_DISPLAY", None)
    get_settings.cache_clear()


@pytest.fixture()
def _dev_otp_default():
    """Pin the default: no env value, freshly resolved settings."""
    os.environ.pop("DEV_OTP_DISPLAY", None)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_flag_off_by_default_response_has_no_dev_otp(
    _dev_otp_default, caplog: pytest.LogCaptureFixture
) -> None:
    async def run() -> None:
        email = unique_email()
        tid, _ = await seed_user(email)
        headers = {"x-tenant-id": str(tid)}
        with caplog.at_level(logging.DEBUG):
            async with api_client() as client:
                res = await client.post("/auth/otp/request", json={"email": email}, headers=headers)
        assert res.status_code == 202
        # FAIL-CLOSED: the key is ABSENT, not empty/null.
        assert res.json() == {"status": "sent"}
        # No six-digit code ever reaches a log line (gate 1.6).
        assert re.search(r"\b\d{6}\b", caplog.text) is None

    asyncio.run(run())


def test_flag_on_displays_a_code_that_actually_verifies(
    _dev_otp_on, caplog: pytest.LogCaptureFixture
) -> None:
    async def run() -> None:
        email = unique_email()
        tid, _ = await seed_user(email)
        headers = {"x-tenant-id": str(tid)}
        with caplog.at_level(logging.DEBUG):
            async with api_client() as client:
                res = await client.post("/auth/otp/request", json={"email": email}, headers=headers)
                assert res.status_code == 202
                body = res.json()
                assert body["status"] == "sent"
                code = body["dev_otp"]
                assert re.fullmatch(r"\d{6}", code) is not None
                # The displayed code is the REAL challenge: it verifies.
                verified = await client.post(
                    "/auth/otp/verify", json={"email": email, "code": code}, headers=headers
                )
                assert verified.status_code == 200
                assert "access_token" in verified.json()
        # Displayed, never LOGGED (gate 1.6).
        assert code not in caplog.text

    asyncio.run(run())


def test_flag_on_unknown_email_returns_bare_sent(_dev_otp_on) -> None:
    async def run() -> None:
        email = unique_email()
        tid, _ = await seed_user(email)
        headers = {"x-tenant-id": str(tid)}
        async with api_client() as client:
            res = await client.post(
                "/auth/otp/request",
                json={"email": f"ghost-{uuid.uuid4().hex[:8]}@nowhere.invalid"},
                headers=headers,
            )
        assert res.status_code == 202
        assert res.json() == {"status": "sent"}

    asyncio.run(run())
