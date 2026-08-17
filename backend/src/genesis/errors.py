"""Application error taxonomy. Clients receive a category, never internals (gate 1.6)."""

from enum import StrEnum


class ErrorCategory(StrEnum):
    VALIDATION = "validation_error"
    NOT_FOUND = "not_found"
    CONFLICT = "conflict"
    UNAUTHENTICATED = "unauthenticated"
    FORBIDDEN = "forbidden"
    RATE_LIMITED = "rate_limited"
    INTERNAL = "internal_error"


class AppError(Exception):
    """Base application error. The message is internal-only."""

    status_code: int = 500
    category: ErrorCategory = ErrorCategory.INTERNAL


class NotFoundError(AppError):
    status_code = 404
    category = ErrorCategory.NOT_FOUND


class ConflictError(AppError):
    """Raised on optimistic-lock version mismatch and duplicates (gate 1.4)."""

    status_code = 409
    category = ErrorCategory.CONFLICT


class InvalidInputError(AppError):
    """Raised when request parameters fail semantic validation (gate 1.2).

    Complements FastAPI's structural 422s for cases only the application
    can judge, such as malformed pagination cursors.
    """

    status_code = 400
    category = ErrorCategory.VALIDATION


class UnprocessableError(AppError):
    """Raised when a structurally valid body fails schema validation
    against server-side state — e.g. a KYC profile payload of the wrong
    member type (P13.12). Mirrors FastAPI's structural 422; the client
    receives the sanitized category only, never the offending values
    (gate 1.6).
    """

    status_code = 422
    category = ErrorCategory.VALIDATION


class ForbiddenError(AppError):
    status_code = 403
    category = ErrorCategory.FORBIDDEN


class UnauthenticatedError(AppError):
    status_code = 401
    category = ErrorCategory.UNAUTHENTICATED


class RateLimitedError(AppError):
    """Raised when an auth-sensitive endpoint is called too often (gate 1.6)."""

    status_code = 429
    category = ErrorCategory.RATE_LIMITED
