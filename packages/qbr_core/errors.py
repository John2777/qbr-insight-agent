from __future__ import annotations


class QBRError(Exception):
    code = "INTERNAL_ERROR"
    status_code = 500
    title = "Internal error"

    def __init__(self, detail: str, *, errors: list[dict[str, object]] | None = None) -> None:
        super().__init__(detail)
        self.detail = detail
        self.errors = errors or []


class UnsupportedFile(QBRError):
    code = "UNSUPPORTED_FILE_TYPE"
    status_code = 415
    title = "Unsupported presentation format"


class UnsafeArchive(QBRError):
    code = "ZIP_BOMB_SUSPECTED"
    status_code = 422
    title = "Unsafe presentation package"


class FileTooLarge(QBRError):
    code = "FILE_TOO_LARGE"
    status_code = 413
    title = "Presentation is too large"


class ResourceNotFound(QBRError):
    code = "RESOURCE_NOT_FOUND"
    status_code = 404
    title = "Resource not found"


class Conflict(QBRError):
    code = "RESOURCE_CONFLICT"
    status_code = 409
    title = "Resource conflict"


class InvalidState(QBRError):
    code = "INVALID_STATE"
    status_code = 409
    title = "Operation is not valid in the current state"


class InsufficientEvidence(QBRError):
    code = "INSUFFICIENT_EVIDENCE"
    status_code = 422
    title = "Insufficient evidence"


class AuthenticationRequired(QBRError):
    code = "AUTHENTICATION_REQUIRED"
    status_code = 401
    title = "Authentication required"


class PermissionDenied(QBRError):
    code = "PERMISSION_DENIED"
    status_code = 403
    title = "Permission denied"


class TooManyRequests(QBRError):
    code = "TOO_MANY_REQUESTS"
    status_code = 429
    title = "Too many requests"
