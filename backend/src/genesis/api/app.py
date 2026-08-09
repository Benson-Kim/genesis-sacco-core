"""Application factory: middleware, error envelope, router wiring."""

import logging
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
from genesis.api.recovery import router as recovery_router
from genesis.api.reports import router as reports_router
from genesis.api.tenant_settings import router as tenant_settings_router
from genesis.api.transactions import router as transactions_router
from genesis.api.users import router as users_router
from genesis.application.pagination import assert_cursor_signing_key_configured
from genesis.errors import AppError, ErrorCategory
from genesis.logging import configure_logging, correlation_id_var
from genesis.settings import get_settings

logger = logging.getLogger("genesis.api")


def _envelope(category: ErrorCategory) -> dict[str, str]:
    return {"category": category.value, "correlation_id": correlation_id_var.get()}


def create_app() -> FastAPI:
    configure_logging()
    # Fail-closed boot guard: a missing
    # or short cursor-signing key aborts startup here, never at the
    # first decode.
    assert_cursor_signing_key_configured()
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
        cid = request.headers.get("x-request-id") or uuid.uuid4().hex
        token = correlation_id_var.set(cid)
        try:
            response = await call_next(request)
        finally:
            correlation_id_var.reset(token)
        response.headers["x-request-id"] = cid
        return response

    @app.exception_handler(AppError)
    async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
        logger.warning("handled error: %s", exc.category.value)
        return JSONResponse(status_code=exc.status_code, content=_envelope(exc.category))

    @app.exception_handler(Exception)
    async def unhandled_handler(request: Request, exc: Exception) -> JSONResponse:
        logger.error("unhandled error", exc_info=exc)
        return JSONResponse(status_code=500, content=_envelope(ErrorCategory.INTERNAL))

    return app
