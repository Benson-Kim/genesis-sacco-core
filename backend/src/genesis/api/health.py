"""Liveness and readiness endpoints (the house doctrine reliability)."""

from fastapi import APIRouter, Response

from genesis.infrastructure.db import ping_db
from genesis.infrastructure.redis_client import ping_redis
from genesis.settings import get_settings

router = APIRouter()


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/readyz")
async def readyz(response: Response) -> dict[str, str]:
    settings = get_settings()
    checks: dict[str, str] = {}
    ok = True

    if settings.database_url:
        db_ok = await ping_db(settings.database_url)
        checks["database"] = "ok" if db_ok else "fail"
        ok = ok and db_ok
    else:
        checks["database"] = "unconfigured"
        ok = False

    if settings.redis_url:
        redis_ok = await ping_redis(settings.redis_url)
        checks["redis"] = "ok" if redis_ok else "fail"
        ok = ok and redis_ok
    else:
        checks["redis"] = "skipped"

    if not ok:
        response.status_code = 503
    checks["status"] = "ready" if ok else "unready"
    return checks
