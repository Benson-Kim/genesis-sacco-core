"""Application factory: middleware, error envelope, router wiring."""

import logging
import re
import time
import uuid
from collections.abc import Awaitable, Callable

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from genesis.api.access import router as access_router
from genesis.api.accounting_periods import jobs_router as accounting_jobs_router
from genesis.api.accounting_periods import router as accounting_periods_router
from genesis.api.audit_log import router as audit_log_router
from genesis.api.auth import router as auth_router
from genesis.api.branches import router as branches_router
from genesis.api.corrections import router as corrections_router
from genesis.api.dashboard import router as dashboard_router
from genesis.api.dividends import router as dividends_router
from genesis.api.health import router as health_router
from genesis.api.idempotency import IdempotencyMiddleware
from genesis.api.loan_book import router as loan_book_router
from genesis.api.loans import router as loans_router
from genesis.api.me import router as me_router
from genesis.api.member import router as member_router
from genesis.api.member_exits import router as member_exits_router
from genesis.api.member_identity import router as member_identity_router
from genesis.api.member_kyc import router as member_kyc_router
from genesis.api.members import router as members_router
from genesis.api.ops import router as ops_router
from genesis.api.recovery import router as recovery_router
from genesis.api.reports import router as reports_router
from genesis.api.tenant_settings import router as tenant_settings_router
from genesis.api.transactions import router as transactions_router
from genesis.api.users import router as users_router
from genesis.application.pagination import assert_cursor_signing_key_configured
from genesis.errors import AppError, ErrorCategory, PayloadSchemaError
from genesis.logging import configure_logging, correlation_id_var
from genesis.observability import (
    AUTH_FAILURES_TOTAL,
    RATE_LIMITED_TOTAL,
    metrics,
    router_label,
)
from genesis.settings import assert_dev_otp_display_dev_only, get_settings

logger = logging.getLogger("genesis.api")

#: Accepted shape for a TRUSTED inbound X-Request-ID (issue #4): strict
#: charset and bounded length, so even a trusted hop can never smuggle
#: log-breaking bytes or unbounded strings into every log record.
_REQUEST_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{7,63}")


def _envelope(category: ErrorCategory) -> dict[str, str]:
    return {"category": category.value, "correlation_id": correlation_id_var.get()}


def create_app() -> FastAPI:
    configure_logging()
    # Fail-closed boot guard: a missing
    # or short cursor-signing key aborts startup here, never at the
    # first decode.
    assert_cursor_signing_key_configured()
    # Fail-closed boot guard (#35): the dev-mode OTP display refuses
    # to boot outside development — the enforced replacement for the
    # old "strip before staging" reminder.
    assert_dev_otp_display_dev_only()
    settings = get_settings()
    app = FastAPI(title="Genesis Prestige API", version="0.1.0")
    if settings.cors_origins_list:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins_list,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )
    app.include_router(health_router)
    app.include_router(auth_router)
    app.include_router(members_router)
    app.include_router(member_kyc_router)
    app.include_router(member_exits_router)
    app.include_router(member_identity_router)
    app.include_router(member_router)
    app.include_router(loans_router)
    app.include_router(loan_book_router)
    app.include_router(recovery_router)
    app.include_router(transactions_router)
    app.include_router(corrections_router)
    app.include_router(dashboard_router)
    app.include_router(dividends_router)
    app.include_router(reports_router)
    app.include_router(tenant_settings_router)
    app.include_router(accounting_periods_router)
    app.include_router(accounting_jobs_router)
    app.include_router(me_router)
    app.include_router(ops_router)
    app.include_router(access_router)
    app.include_router(users_router)
    app.include_router(audit_log_router)
    app.include_router(branches_router)
    app.add_middleware(IdempotencyMiddleware)

    @app.middleware("http")
    async def correlation(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        # Inbound X-Request-ID is honored ONLY from a trusted hop
        # (settings.trust_request_id_header, fail-closed default off)
        # AND only in a strict shape — anything else gets a fresh
        # server-generated id (issue #4: no caller-forged correlation).
        inbound = request.headers.get("x-request-id", "")
        if settings.trust_request_id_header and _REQUEST_ID_RE.fullmatch(inbound):
            cid = inbound
        else:
            cid = uuid.uuid4().hex
        token = correlation_id_var.set(cid)
        started = time.perf_counter()
        try:
            response = await call_next(request)
        finally:
            # Latency lands in the per-router histogram keyed by the
            # matched route TEMPLATE's first segment (bounded
            # cardinality; no path parameters — genesis.observability).
            route = request.scope.get("route")
            metrics.observe_request(
                router_label(getattr(route, "path", None)),
                time.perf_counter() - started,
            )
            correlation_id_var.reset(token)
        response.headers["x-request-id"] = cid
        return response

    @app.exception_handler(AppError)
    async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
        # Rate-limit trips and auth failures are counted HERE, at the
        # error-handler seam — never inside infrastructure/rate_limit.py
        # or api/auth.py internals (both owned by the open !3): every
        # 429/401 the API emits funnels through this one handler.
        if exc.status_code == 429:
            metrics.inc_counter(RATE_LIMITED_TOTAL)
        elif exc.status_code == 401:
            metrics.inc_counter(AUTH_FAILURES_TOTAL)
        logger.warning("handled error: %s", exc.category.value)
        content: dict[str, str] = _envelope(exc.category)
        if isinstance(exc, PayloadSchemaError):
            # Schema-refusal detail travels (mirroring FastAPI's structural
            # 422 detail): PayloadSchemaError messages are code-owned prose
            # naming field LOCATIONS and error TYPES only — never a
            # submitted value or figure (the least-disclosure discipline
            # the class contract pins). Every other AppError — including
            # plain UnprocessableError — stays a category-only envelope.
            content["detail"] = str(exc)
        return JSONResponse(status_code=exc.status_code, content=content)

    @app.exception_handler(Exception)
    async def unhandled_handler(request: Request, exc: Exception) -> JSONResponse:
        logger.error("unhandled error", exc_info=exc)
        return JSONResponse(status_code=500, content=_envelope(ErrorCategory.INTERNAL))

    return app
