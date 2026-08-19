from fastapi.testclient import TestClient


def test_healthz(client: TestClient) -> None:
    res = client.get("/healthz")
    assert res.status_code == 200
    assert res.json() == {"status": "ok"}


def test_correlation_id_inbound_header_is_ignored_by_default(client: TestClient) -> None:
    """Issue #4 fail-closed default: X-Request-ID from an untrusted caller
    is never honored — the server generates its own id (trusted-hop
    echo is covered in test_observability.py)."""
    res = client.get("/healthz", headers={"x-request-id": "attacker-chosen-id-1234"})
    assert res.headers["x-request-id"] != "attacker-chosen-id-1234"
    assert len(res.headers["x-request-id"]) == 32


def test_correlation_id_is_generated_when_absent(client: TestClient) -> None:
    res = client.get("/healthz")
    assert len(res.headers["x-request-id"]) == 32
