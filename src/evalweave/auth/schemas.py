import re
from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from evalweave.db.models import SystemRole


class InputModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


EMAIL_PATTERN = re.compile(
    r"^[A-Z0-9.!#$%&'*+/=?^_`{|}~-]+@"
    r"[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?"
    r"(?:\.[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?)+$",
    re.IGNORECASE,
)


def normalize_email(value: str) -> str:
    value = value.strip().lower()
    local = value.partition("@")[0]
    if not EMAIL_PATTERN.fullmatch(value) or local.startswith(".") or ".." in local:
        raise ValueError("请输入有效的邮箱地址")
    return value


class LoginRequest(InputModel):
    username: str = Field(min_length=3, max_length=64)
    password: str = Field(min_length=8, max_length=128)


class RegisterRequest(LoginRequest):
    email: str = Field(min_length=5, max_length=255)
    user_type_id: UUID

    @field_validator("username")
    @classmethod
    def clean_username(cls, value: str) -> str:
        value = value.strip()
        if any(character.isspace() for character in value):
            raise ValueError("用户名不能包含空格")
        return value

    @field_validator("email")
    @classmethod
    def clean_email(cls, value: str) -> str:
        return normalize_email(value)


class UserTypeCreate(InputModel):
    code: str = Field(min_length=2, max_length=64, pattern=r"^[a-z][a-z0-9_-]*$")
    name: str = Field(min_length=2, max_length=64)
    description: str | None = None
    permissions: list[str] = Field(default_factory=list)
    selectable_on_registration: bool = True


class UserTypeUpdate(InputModel):
    name: str | None = Field(default=None, min_length=2, max_length=64)
    description: str | None = None
    permissions: list[str] | None = None
    selectable_on_registration: bool | None = None
    is_active: bool | None = None


class UserTypeRead(BaseModel):
    id: UUID
    code: str
    name: str
    description: str | None
    permissions: list[str]
    selectable_on_registration: bool
    is_active: bool


class UserCreate(LoginRequest):
    email: str = Field(min_length=5, max_length=255)
    user_type_id: UUID | None = None
    system_role: SystemRole = SystemRole.USER
    is_active: bool = True

    @field_validator("email")
    @classmethod
    def clean_email(cls, value: str) -> str:
        return normalize_email(value)


class UserUpdate(InputModel):
    user_type_id: UUID | None = None
    email: str | None = Field(default=None, min_length=5, max_length=255)
    password: str | None = Field(default=None, min_length=8, max_length=128)
    is_active: bool | None = None

    @field_validator("email")
    @classmethod
    def clean_email(cls, value: str | None) -> str | None:
        return normalize_email(value) if value is not None else None


class UserRead(BaseModel):
    id: UUID
    username: str
    email: str | None
    system_role: SystemRole
    user_type_id: UUID | None
    user_type_name: str | None
    permissions: list[str]
    is_active: bool
    created_at: datetime
    last_login_at: datetime | None
