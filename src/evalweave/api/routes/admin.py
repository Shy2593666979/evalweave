from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlmodel import select

from evalweave.agents.model_config import encrypt_api_key
from evalweave.auth.dependencies import AdminUser, SessionDependency
from evalweave.auth.permissions import permission_catalog
from evalweave.auth.schemas import (
    UserCreate,
    UserRead,
    UserTypeCreate,
    UserTypeRead,
    UserTypeUpdate,
    UserUpdate,
)
from evalweave.auth.security import hash_password
from evalweave.auth.service import (
    assign_default_project,
    commit_or_conflict,
    ensure_user_type,
    user_to_read,
    validate_permissions,
)
from evalweave.db.models import EvaluationModel, SystemRole, User, UserType

router = APIRouter(prefix="/admin", tags=["administration"])


class EvaluationModelCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    base_url: str = Field(min_length=1, max_length=512)
    model_name: str = Field(min_length=1, max_length=128)
    api_mode: Literal["responses", "chat_completions"] = "responses"
    api_key: str = Field(min_length=1, max_length=4096)
    is_active: bool = True


class EvaluationModelUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    base_url: str | None = Field(default=None, min_length=1, max_length=512)
    model_name: str | None = Field(default=None, min_length=1, max_length=128)
    api_mode: Literal["responses", "chat_completions"] | None = None
    api_key: str | None = Field(default=None, min_length=1, max_length=4096)
    is_active: bool | None = None


class EvaluationModelRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    base_url: str
    model_name: str
    api_mode: str
    is_active: bool
    api_key_configured: bool = True
    created_at: datetime
    updated_at: datetime


@router.get("/permissions")
def list_permissions(_: AdminUser) -> list[dict[str, str]]:
    return permission_catalog()


@router.get("/evaluation-models", response_model=list[EvaluationModelRead])
def list_evaluation_models(
    _: AdminUser, session: SessionDependency
) -> list[EvaluationModelRead]:
    models = session.exec(select(EvaluationModel).order_by(EvaluationModel.created_at.desc())).all()
    return [EvaluationModelRead.model_validate(model) for model in models]


@router.post(
    "/evaluation-models",
    response_model=EvaluationModelRead,
    status_code=status.HTTP_201_CREATED,
)
def create_evaluation_model(
    payload: EvaluationModelCreate, _: AdminUser, session: SessionDependency
) -> EvaluationModel:
    model = EvaluationModel(
        name=payload.name.strip(),
        base_url=payload.base_url.strip().rstrip("/"),
        model_name=payload.model_name.strip(),
        api_mode=payload.api_mode,
        api_key_encrypted=encrypt_api_key(payload.api_key),
        is_active=payload.is_active,
    )
    session.add(model)
    commit_or_conflict(session, "评测模型名称已存在")
    session.refresh(model)
    return model


@router.patch("/evaluation-models/{model_id}", response_model=EvaluationModelRead)
def update_evaluation_model(
    model_id: UUID,
    payload: EvaluationModelUpdate,
    _: AdminUser,
    session: SessionDependency,
) -> EvaluationModel:
    model = session.get(EvaluationModel, model_id)
    if model is None:
        raise HTTPException(status_code=404, detail="评测模型不存在")
    changes = payload.model_dump(exclude_unset=True)
    if api_key := changes.pop("api_key", None):
        model.api_key_encrypted = encrypt_api_key(api_key)
    for key, value in changes.items():
        if isinstance(value, str):
            value = value.strip()
        if key == "base_url" and isinstance(value, str):
            value = value.rstrip("/")
        setattr(model, key, value)
    model.updated_at = datetime.now(UTC)
    session.add(model)
    commit_or_conflict(session, "评测模型名称已存在")
    session.refresh(model)
    return model


@router.get("/user-types", response_model=list[UserTypeRead])
def list_user_types(_: AdminUser, session: SessionDependency) -> list[UserType]:
    return list(session.exec(select(UserType).order_by(UserType.created_at)).all())


@router.post("/user-types", response_model=UserTypeRead, status_code=status.HTTP_201_CREATED)
def create_user_type(
    payload: UserTypeCreate,
    _: AdminUser,
    session: SessionDependency,
) -> UserType:
    user_type = UserType(
        code=payload.code,
        name=payload.name.strip(),
        description=payload.description,
        permissions=validate_permissions(payload.permissions),
        selectable_on_registration=payload.selectable_on_registration,
    )
    session.add(user_type)
    commit_or_conflict(session, "用户类型编码或名称已存在")
    session.refresh(user_type)
    return user_type


@router.patch("/user-types/{user_type_id}", response_model=UserTypeRead)
def update_user_type(
    user_type_id: UUID,
    payload: UserTypeUpdate,
    _: AdminUser,
    session: SessionDependency,
) -> UserType:
    user_type = session.get(UserType, user_type_id)
    if user_type is None:
        raise HTTPException(status_code=404, detail="用户类型不存在")
    changes = payload.model_dump(exclude_unset=True)
    if permissions := changes.get("permissions"):
        changes["permissions"] = validate_permissions(permissions)
    elif "permissions" in changes:
        changes["permissions"] = []
    for key, value in changes.items():
        setattr(user_type, key, value)
    user_type.updated_at = datetime.now(UTC)
    session.add(user_type)
    commit_or_conflict(session, "用户类型名称已存在")
    session.refresh(user_type)
    return user_type


@router.get("/users", response_model=list[UserRead])
def list_users(_: AdminUser, session: SessionDependency) -> list[UserRead]:
    users = session.exec(select(User).order_by(User.created_at.desc())).all()
    return [user_to_read(session, user) for user in users]


@router.post("/users", response_model=UserRead, status_code=status.HTTP_201_CREATED)
def create_user(payload: UserCreate, _: AdminUser, session: SessionDependency) -> UserRead:
    if payload.system_role == SystemRole.USER:
        if payload.user_type_id is None:
            raise HTTPException(status_code=422, detail="普通用户必须选择用户类型")
        ensure_user_type(session, payload.user_type_id)
    user = User(
        username=payload.username.strip(),
        email=payload.email,
        password_hash=hash_password(payload.password),
        system_role=payload.system_role,
        user_type_id=payload.user_type_id if payload.system_role == SystemRole.USER else None,
        is_active=payload.is_active,
    )
    session.add(user)
    commit_or_conflict(session, "用户名或邮箱已存在")
    session.refresh(user)
    assign_default_project(session, user)
    return user_to_read(session, user)


@router.patch("/users/{user_id}", response_model=UserRead)
def update_user(
    user_id: UUID,
    payload: UserUpdate,
    admin: AdminUser,
    session: SessionDependency,
) -> UserRead:
    user = session.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="用户不存在")
    changes = payload.model_dump(exclude_unset=True)
    if user.system_role == SystemRole.USER and "user_type_id" in changes:
        if changes["user_type_id"] is None:
            raise HTTPException(status_code=422, detail="普通用户必须选择用户类型")
        ensure_user_type(session, changes["user_type_id"])
        user.user_type_id = changes["user_type_id"]
    if "email" in changes:
        user.email = changes["email"]
    if password := changes.get("password"):
        user.password_hash = hash_password(password)
    if "is_active" in changes:
        if user.id == admin.id and not changes["is_active"]:
            raise HTTPException(status_code=422, detail="不能停用当前登录的 Admin")
        user.is_active = changes["is_active"]
    user.updated_at = datetime.now(UTC)
    session.add(user)
    commit_or_conflict(session, "邮箱已被其他用户使用")
    session.refresh(user)
    return user_to_read(session, user)
