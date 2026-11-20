"""Centralised API error model: every error response is {"error": {"code", "message", "request_id"}}."""
from __future__ import annotations

import logging
import uuid

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.orm.exc import StaleDataError

log = logging.getLogger("faultlens.api")


class AppError(Exception):
    status_code = 400
    code = "bad_request"

    def __init__(self, message: str, *, code: str | None = None, status_code: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        if code:
            self.code = code
        if status_code:
            self.status_code = status_code


class NotFound(AppError):
    status_code, code = 404, "not_found"


class Conflict(AppError):
    status_code, code = 409, "conflict"


class Forbidden(AppError):
    status_code, code = 403, "forbidden"


class Unauthorized(AppError):
    status_code, code = 401, "unauthorized"


class ServiceUnavailable(AppError):
    status_code, code = 503, "dependency_unavailable"


class TooManyRequests(AppError):
    status_code, code = 429, "rate_limited"


def _rid(request: Request) -> str:
    return getattr(request.state, "request_id", None) or uuid.uuid4().hex


def _body(code: str, message: str, request: Request, **extra) -> dict:
    return {"error": {"code": code, "message": message, "request_id": _rid(request), **extra}}


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error(request: Request, exc: AppError):  # noqa: ANN202
        headers = {"WWW-Authenticate": "Bearer"} if exc.status_code == 401 else None
        return JSONResponse(_body(exc.code, exc.message, request), status_code=exc.status_code, headers=headers)

    @app.exception_handler(HTTPException)
    async def _http(request: Request, exc: HTTPException):  # noqa: ANN202
        return JSONResponse(_body("http_error", str(exc.detail), request), status_code=exc.status_code)

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError):  # noqa: ANN202
        details = [{"loc": list(e["loc"]), "msg": e["msg"], "type": e["type"]} for e in exc.errors()]
        return JSONResponse(_body("validation_error", "request validation failed", request, details=details), status_code=422)

    @app.exception_handler(StaleDataError)
    async def _stale(request: Request, exc: StaleDataError):  # noqa: ANN202
        return JSONResponse(_body("conflict", "resource was modified concurrently; retry", request), status_code=409)

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception):  # noqa: ANN202
        log.exception("unhandled error", extra={"path": request.url.path})
        return JSONResponse(_body("internal_error", "internal server error", request), status_code=500)
