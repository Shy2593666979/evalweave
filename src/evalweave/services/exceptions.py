class ServiceError(Exception):
    status_code = 400
    code: int | str = 400

    def __init__(self, message: str, *, code: int | str | None = None) -> None:
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code


class NotFoundError(ServiceError):
    status_code = 404
    code = 404


class ForbiddenError(ServiceError):
    status_code = 403
    code = 403


class ConflictError(ServiceError):
    status_code = 409
    code = 409


class ValidationError(ServiceError):
    status_code = 422
    code = 422


class PayloadTooLargeError(ServiceError):
    status_code = 413
    code = 413
