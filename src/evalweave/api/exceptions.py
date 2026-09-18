from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

from evalweave.api.response import APIResponse
from evalweave.services.exceptions import ServiceError


async def handle_service_error(_: Request, exc: ServiceError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content=APIResponse.fail(message=exc.message, code=exc.code).model_dump(
            mode="json"
        ),
    )


async def handle_http_error(_: Request, exc: HTTPException) -> JSONResponse:
    message = exc.detail if isinstance(exc.detail, str) else "请求失败"
    return JSONResponse(
        status_code=exc.status_code,
        content=APIResponse.fail(
            message=message,
            code=exc.status_code,
            data=None if isinstance(exc.detail, str) else exc.detail,
        ).model_dump(mode="json"),
        headers=exc.headers,
    )


async def handle_validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
    errors = [
        {key: value for key, value in item.items() if key != "ctx"}
        for item in exc.errors()
    ]
    return JSONResponse(
        status_code=422,
        content=APIResponse.fail(
            message="请求参数错误",
            code=422,
            data=errors,
        ).model_dump(mode="json"),
    )


def register_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(ServiceError, handle_service_error)
    app.add_exception_handler(HTTPException, handle_http_error)
    app.add_exception_handler(RequestValidationError, handle_validation_error)
