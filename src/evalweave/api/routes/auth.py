from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Response, status
from sqlmodel import select

from evalweave.auth.dependencies import CurrentUser, SessionDependency
from evalweave.auth.schemas import LoginRequest, RegisterRequest, UserRead, UserTypeRead
from evalweave.auth.security import create_access_token, hash_password, verify_password
from evalweave.auth.service import (
    commit_or_conflict,
    ensure_user_type,
    user_to_read,
)
from evalweave.core.config import get_settings
from evalweave.db.models import SystemRole, User, UserType

router = APIRouter(prefix="/auth", tags=["authentication"])


@router.get("/registration-options", response_model=list[UserTypeRead])
def registration_options(session: SessionDependency) -> list[UserType]:
    statement = select(UserType).where(
        UserType.is_active.is_(True),
        UserType.selectable_on_registration.is_(True),
    )
    return list(session.exec(statement.order_by(UserType.name)).all())


@router.post("/register", response_model=UserRead, status_code=status.HTTP_201_CREATED)
def register(payload: RegisterRequest, session: SessionDependency) -> UserRead:
    if not get_settings().auth.allow_registration:
        raise HTTPException(status_code=403, detail="系统已关闭公开注册")
    ensure_user_type(session, payload.user_type_id, registration=True)
    user = User(
        username=payload.username,
        password_hash=hash_password(payload.password),
        system_role=SystemRole.USER,
        user_type_id=payload.user_type_id,
    )
    session.add(user)
    commit_or_conflict(session, "用户名已存在")
    session.refresh(user)
    return user_to_read(session, user)


@router.post("/login", response_model=UserRead)
def login(payload: LoginRequest, response: Response, session: SessionDependency) -> UserRead:
    username = payload.username.strip()
    user = session.exec(select(User).where(User.username == username)).first()
    if user is None or not verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    if not user.is_active:
        raise HTTPException(status_code=403, detail="账号已被停用")

    user.last_login_at = datetime.now(UTC)
    session.add(user)
    session.commit()
    session.refresh(user)

    auth = get_settings().auth
    response.set_cookie(
        key=auth.cookie_name,
        value=create_access_token(user.id),
        httponly=True,
        secure=auth.cookie_secure,
        samesite="lax",
        max_age=auth.token_expire_minutes * 60,
        path="/",
    )
    return user_to_read(session, user)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(response: Response) -> None:
    response.delete_cookie(get_settings().auth.cookie_name, path="/")


@router.get("/me", response_model=UserRead)
def me(user: CurrentUser, session: SessionDependency) -> UserRead:
    return user_to_read(session, user)
