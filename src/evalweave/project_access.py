from uuid import UUID

from fastapi import HTTPException
from sqlmodel import Session, select

from evalweave.db.models import Project, ProjectMember, SystemRole, User


def project_ids_for_user(session: Session, user: User) -> set[UUID] | None:
    """Return accessible project ids, or None when the user may access every project."""
    if user.system_role == SystemRole.ADMIN:
        return None
    return set(
        session.exec(
            select(ProjectMember.project_id).where(ProjectMember.user_id == user.id)
        ).all()
    )


def require_project_access(session: Session, user: User, project_id: UUID) -> Project:
    project = session.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="项目不存在")
    allowed = project_ids_for_user(session, user)
    if allowed is not None and project_id not in allowed:
        raise HTTPException(status_code=403, detail="你没有访问该项目的权限")
    return project
