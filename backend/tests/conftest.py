import os
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from genesis.api.app import create_app

#: DATABASE_MAINT_URL fence (issue #33 item 1, from the !7 review).
#:
#: DATABASE_MAINT_URL is the RLS-OWNER DSN, exported by backend:test
#: solely so the EXPLAIN structural gates can ANALYZE seeded books
#: (PG16 has no MAINTAIN privilege for the unprivileged app role).
#: Left ambient in the whole job env, any future test could quietly
#: read THROUGH row-level security with it — and the tenant-isolation
#: / IDOR proofs would keep passing while proving less than they
#: claim. The autouse fixture below swaps the variable for a sentinel
#: DSN for every test module EXCEPT this explicit allowlist: the
#: sentinel's host lives under the RFC 2606 .invalid TLD, so a fenced
#: test that tries to connect fails loudly with the fence message AS
#: the unresolvable host name in the error. Extend the allowlist
#: deliberately (the diff is reviewable); never read
#: DATABASE_MAINT_URL anywhere else.
MAINT_DSN_ALLOWED_MODULES = frozenset({"test_member_portal_explain"})

MAINT_DSN_FENCE_SENTINEL = (
    "postgresql+psycopg://fenced:fenced@"
    "database-maint-url-is-fenced-to-the-explain-modules"
    ".see-backend-tests-conftest-issue-33.invalid/fenced"
)


@pytest.fixture(autouse=True)
def _maint_dsn_fence(request: pytest.FixtureRequest) -> Iterator[None]:
    """Restrict the owner DSN to the allowlisted EXPLAIN modules."""
    real = os.environ.get("DATABASE_MAINT_URL")
    if real is None:
        yield
        return
    module = getattr(request, "module", None)
    if module is not None and module.__name__ in MAINT_DSN_ALLOWED_MODULES:
        yield
        return
    os.environ["DATABASE_MAINT_URL"] = MAINT_DSN_FENCE_SENTINEL
    try:
        yield
    finally:
        os.environ["DATABASE_MAINT_URL"] = real


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
