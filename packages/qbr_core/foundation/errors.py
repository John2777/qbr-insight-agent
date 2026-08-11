from __future__ import annotations


class QBRError(Exception):
    """Base exception carrying a stable API error code and HTTP status."""
    code = "INTERNAL_ERROR"
    status_code = 500
    title = "Internal error"

    def __init__(self, detail: str, *, errors: list[dict[str, object]] | None = None) -> None:
        """Initialize an API error with detail and optional structured causes."""
        super().__init__(detail)
        self.detail = detail
        self.errors = errors or []


class UnsupportedFile(QBRError):
    """Signal that an uploaded file uses an unsupported presentation format."""
    code = "UNSUPPORTED_FILE_TYPE"
    status_code = 415
    title = "Unsupported presentation format"


class UnsafeArchive(QBRError):
    """Signal that an OOXML archive violates extraction safety limits."""
    code = "ZIP_BOMB_SUSPECTED"
    status_code = 422
    title = "Unsafe presentation package"


class FileTooLarge(QBRError):
    """Signal that an upload exceeds the configured request size limit."""
    code = "FILE_TOO_LARGE"
    status_code = 413
    title = "Presentation is too large"


class ResourceNotFound(QBRError):
    """Signal that a scoped application resource does not exist."""
    code = "RESOURCE_NOT_FOUND"
    status_code = 404
    title = "Resource not found"


class Conflict(QBRError):
    """Signal that an operation conflicts with current persisted state."""
    code = "RESOURCE_CONFLICT"
    status_code = 409
    title = "Resource conflict"


class InvalidState(QBRError):
    """Signal that a resource cannot perform the requested state transition."""
    code = "INVALID_STATE"
    status_code = 409
    title = "Operation is not valid in the current state"


class InsufficientEvidence(QBRError):
    """Signal that available evidence cannot support a requested answer."""
    code = "INSUFFICIENT_EVIDENCE"
    status_code = 422
    title = "Insufficient evidence"


class AuthenticationRequired(QBRError):
    """Signal that a request requires an authenticated principal."""
    code = "AUTHENTICATION_REQUIRED"
    status_code = 401
    title = "Authentication required"


class PermissionDenied(QBRError):
    """Signal that a principal lacks the required authorization scope."""
    code = "PERMISSION_DENIED"
    status_code = 403
    title = "Permission denied"


class TooManyRequests(QBRError):
    """Signal that a caller has exceeded an application rate limit."""
    code = "TOO_MANY_REQUESTS"
    status_code = 429
    title = "Too many requests"
