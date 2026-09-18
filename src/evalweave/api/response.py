from __future__ import annotations

from typing import Any

from pydantic import BaseModel


class APIResponse[T](BaseModel):
    """Build the JSON envelope used by API-only responses.

    Services must return domain values or raise business exceptions; they must
    never depend on this HTTP representation.
    """

    code: int | str
    message: str
    data: T | None = None

    @classmethod
    def success(cls, data: T | None = None, message: str = "操作成功") -> APIResponse[T]:
        return cls(code=0, message=message, data=data)

    @classmethod
    def fail(
        cls,
        message: str = "操作失败",
        *,
        code: int | str = 400,
        data: Any = None,
    ) -> APIResponse[Any]:
        return cls(code=code, message=message, data=data)
