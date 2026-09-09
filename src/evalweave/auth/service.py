from uuid import UUID

from fastapi import HTTPException, status
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from evalweave.auth.permissions import DEFAULT_USER_TYPES, Permission
from evalweave.auth.schemas import UserRead
from evalweave.auth.security import hash_password
from evalweave.core.config import get_settings
from evalweave.db.models import SystemRole, User, UserType
from evalweave.db.session import get_engine


def validate_permissions(permissions: list[str]) -> list[str]:
    allowed = {item.value for item in Permission}
    invalid = sorted(set(permissions) - allowed)
    if invalid:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"未知权限: {', '.join(invalid)}",
        )
    return sorted(set(permissions))


def ensure_user_type(
    session: Session, user_type_id: UUID, *, registration: bool = False
) -> UserType:
    user_type = session.get(UserType, user_type_id)
    if user_type is None or not user_type.is_active:
        raise HTTPException(status_code=422, detail="用户类型不存在或已停用")
    if registration and not user_type.selectable_on_registration:
        raise HTTPException(status_code=422, detail="该用户类型不可用于注册")
    return user_type


def commit_or_conflict(session: Session, message: str) -> None:
    try:
        session.commit()
    except IntegrityError as error:
        session.rollback()
        raise HTTPException(status_code=409, detail=message) from error


def user_to_read(session: Session, user: User) -> UserRead:
    user_type = session.get(UserType, user.user_type_id) if user.user_type_id else None
    permissions = (
        [item.value for item in Permission] if user.system_role == SystemRole.ADMIN else []
    )
    if user_type is not None:
        permissions = user_type.permissions
    return UserRead(
        id=user.id,
        username=user.username,
        system_role=user.system_role,
        user_type_id=user.user_type_id,
        user_type_name=user_type.name if user_type else None,
        permissions=permissions,
        is_active=user.is_active,
        created_at=user.created_at,
        last_login_at=user.last_login_at,
    )


def bootstrap_identity_data() -> None:
    settings = get_settings()
    with Session(get_engine()) as session:
        for definition in DEFAULT_USER_TYPES:
            existing = session.exec(
                select(UserType).where(UserType.code == definition["code"])
            ).first()
            if existing is None:
                session.add(
                    UserType(
                        code=definition["code"],
                        name=definition["name"],
                        description=definition["description"],
                        permissions=[item.value for item in definition["permissions"]],
                    )
                )
        session.commit()

        admin_config = settings.auth.bootstrap_admin
        if not admin_config.enabled:
            return
        admin = session.exec(select(User).where(User.system_role == SystemRole.ADMIN)).first()
        if admin is None:
            session.add(
                User(
                    username=admin_config.username.strip(),
                    password_hash=hash_password(admin_config.password),
                    system_role=SystemRole.ADMIN,
                )
            )
            commit_or_conflict(session, "初始化 Admin 用户失败，用户名已存在")
