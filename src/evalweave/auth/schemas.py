from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from evalweave.db.models import SystemRole


class InputModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LoginRequest(InputModel):
    username: str = Field(min_length=3, max_length=64)
    password: str = Field(min_length=8, max_length=128)


class RegisterRequest(LoginRequest):
    user_type_id: UUID

    @field_validator("username")
    @classmethod
    def clean_username(cls, value: str) -> str:
        value = value.strip()
        if any(character.isspace() for character in value):
            raise ValueError("用户名不能包含空格")
        return value


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
    user_type_id: UUID | None = None
    system_role: SystemRole = SystemRole.USER
    is_active: bool = True


class UserUpdate(InputModel):
    user_type_id: UUID | None = None
    password: str | None = Field(default=None, min_length=8, max_length=128)
    is_active: bool | None = None


class UserRead(BaseModel):
    id: UUID
    username: str
    system_role: SystemRole
    user_type_id: UUID | None
    user_type_name: str | None
    permissions: list[str]
    is_active: bool
    created_at: datetime
    last_login_at: datetime | None
