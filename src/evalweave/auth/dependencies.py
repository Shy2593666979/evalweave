from collections.abc import Callable
from typing import Annotated

import jwt
from fastapi import Depends, HTTPException, Request, status
from sqlmodel import Session

from evalweave.auth.permissions import Permission
from evalweave.auth.security import decode_access_token
from evalweave.core.config import get_settings
from evalweave.db.models import SystemRole, User, UserType
from evalweave.db.session import get_session

SessionDependency = Annotated[Session, Depends(get_session)]


def current_user(request: Request, session: SessionDependency) -> User:
    token = request.cookies.get(get_settings().auth.cookie_name)
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="请先登录")
    try:
        user_id = decode_access_token(token)
    except (jwt.InvalidTokenError, ValueError, KeyError) as error:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="登录状态已失效"
        ) from error
    user = session.get(User, user_id)
    if user is None or not user.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="用户不存在或已停用")
    return user


CurrentUser = Annotated[User, Depends(current_user)]


def require_admin(user: CurrentUser) -> User:
    if user.system_role != SystemRole.ADMIN:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="需要 Admin 权限")
    return user


AdminUser = Annotated[User, Depends(require_admin)]


def require_permission(permission: Permission) -> Callable[..., User]:
    def dependency(user: CurrentUser, session: SessionDependency) -> User:
        if user.system_role == SystemRole.ADMIN:
            return user
        user_type = session.get(UserType, user.user_type_id) if user.user_type_id else None
        if user_type is None or permission.value not in user_type.permissions:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, detail="当前用户没有此操作权限"
            )
        return user

    return dependency
