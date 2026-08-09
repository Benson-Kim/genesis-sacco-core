import os

import pytest
from fastapi.testclient import TestClient

from genesis.api.app import create_app


@pytest.fixture(autouse=True, scope="session")
def _auth_env() -> None:
    """Test-only key material; real values come from CI/CD variables (gate 1.6)."""
    os.environ.setdefault("JWT_SIGNING_KEY", "test-only-signing-key")
    os.environ.setdefault("OTP_PEPPER", "test-only-pepper")
    # >= 32 bytes: the B13-R5 boot guard rejects shorter key material.
    # Dual-version rotation window (review B13-R10): the whole suite
    # runs with BOTH an active (v2) and a previous (v1) key configured
    # so every API-level decode exercises the dual-key map.
    os.environ.setdefault("CURSOR_SIGNING_KEY", "test-only-cursor-signing-key-0123456789")
    os.environ.setdefault("CURSOR_KEY_VERSION", "2")
    os.environ.setdefault("CURSOR_SIGNING_KEY_PREVIOUS", "test-only-previous-cursor-key-987654321")
    os.environ.setdefault("CURSOR_KEY_VERSION_PREVIOUS", "1")


@pytest.fixture()
def client() -> TestClient:
    return TestClient(create_app(), raise_server_exceptions=False)
