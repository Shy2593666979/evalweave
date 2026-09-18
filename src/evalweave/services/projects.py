from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlmodel import Session, select

from evalweave.db.models import Project, ProjectMember, SystemRole, User
from evalweave.services.exceptions import ForbiddenError, NotFoundError, ValidationError


def clean_optional(value: str | None) -> str | None:
    """Normalize an optional text field without requiring service state."""
    cleaned = value.strip() if value else ""
    return cleaned or None


class ProjectService:
    @staticmethod
    def accessible_ids(session: Session, user: User) -> set[UUID] | None:
        if user.system_role == SystemRole.ADMIN:
            return None
        return set(
            session.exec(
                select(ProjectMember.project_id).where(ProjectMember.user_id == user.id)
            ).all()
        )

    @staticmethod
    def require_access(session: Session, user: User, project_id: UUID) -> Project:
        project = session.get(Project, project_id)
        if project is None:
            raise NotFoundError("项目不存在")
        allowed = ProjectService.accessible_ids(session, user)
        if allowed is not None and project_id not in allowed:
            raise ForbiddenError("你没有访问该项目的权限")
        return project

    @staticmethod
    def list(session: Session, user: User) -> list[Project]:
        statement = select(Project).order_by(Project.created_at)
        allowed = ProjectService.accessible_ids(session, user)
        if allowed is not None:
            if not allowed:
                return []
            statement = statement.where(Project.id.in_(allowed))
        return list(session.exec(statement).all())

    @staticmethod
    def create(session: Session, values: dict[str, Any]) -> Project:
        project = Project(
            name=str(values["name"]).strip(),
            description=clean_optional(values.get("description")),
            service_url=clean_optional(values.get("service_url")),
            agent_context=clean_optional(values.get("agent_context")),
        )
        session.add(project)
        session.commit()
        session.refresh(project)
        return project

    @staticmethod
    def update(
        session: Session,
        project_id: UUID,
        changes: dict[str, Any],
    ) -> Project:
        project = session.get(Project, project_id)
        if project is None:
            raise NotFoundError("项目不存在")
        normalized = dict(changes)
        if "name" in normalized:
            normalized["name"] = normalized["name"].strip()
        for field in ("description", "service_url", "agent_context"):
            if field in normalized:
                normalized[field] = clean_optional(normalized[field])
        for key, value in normalized.items():
            setattr(project, key, value)
        project.updated_at = datetime.now(UTC)
        session.add(project)
        session.commit()
        session.refresh(project)
        return project

    @staticmethod
    def list_members(session: Session, project_id: UUID) -> list[UUID]:
        if session.get(Project, project_id) is None:
            raise NotFoundError("项目不存在")
        return list(
            session.exec(
                select(ProjectMember.user_id).where(ProjectMember.project_id == project_id)
            ).all()
        )

    @staticmethod
    def replace_members(
        session: Session,
        project_id: UUID,
        user_ids: list[UUID],
    ) -> list[UUID]:
        if session.get(Project, project_id) is None:
            raise NotFoundError("项目不存在")
        unique_ids = list(dict.fromkeys(user_ids))
        if unique_ids:
            users = session.exec(
                select(User).where(
                    User.id.in_(unique_ids),
                    User.system_role == SystemRole.USER,
                    User.is_active == True,  # noqa: E712
                )
            ).all()
            if {user.id for user in users} != set(unique_ids):
                raise ValidationError("成员中包含不存在或不可用的普通用户")
        existing = session.exec(
            select(ProjectMember).where(ProjectMember.project_id == project_id)
        ).all()
        existing_by_user = {membership.user_id: membership for membership in existing}
        requested_ids = set(unique_ids)
        for user_id, membership in existing_by_user.items():
            if user_id not in requested_ids:
                session.delete(membership)
        for user_id in requested_ids - set(existing_by_user):
            session.add(ProjectMember(project_id=project_id, user_id=user_id))
        session.commit()
        return unique_ids
