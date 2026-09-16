from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlmodel import Session, select

from evalweave.auth.dependencies import AdminUser, require_permission
from evalweave.auth.permissions import Permission
from evalweave.db.models import Project, ProjectMember, SystemRole, User
from evalweave.db.session import get_session
from evalweave.project_access import project_ids_for_user, require_project_access

router = APIRouter(prefix="/projects", tags=["projects"])

SessionDependency = Annotated[Session, Depends(get_session)]
ProjectReader = Annotated[User, Depends(require_permission(Permission.PROJECT_READ))]


class ProjectPayload(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    description: str | None = Field(default=None, max_length=2000)
    service_url: str | None = Field(default=None, max_length=512)
    agent_context: str | None = Field(default=None, max_length=8000)


class ProjectUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    description: str | None = Field(default=None, max_length=2000)
    service_url: str | None = Field(default=None, max_length=512)
    agent_context: str | None = Field(default=None, max_length=8000)


class ProjectRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    description: str | None
    service_url: str | None
    agent_context: str | None
    created_at: datetime
    updated_at: datetime


class ProjectMembersUpdate(BaseModel):
    user_ids: list[UUID] = Field(default_factory=list)


def clean_optional(value: str | None) -> str | None:
    cleaned = value.strip() if value else ""
    return cleaned or None


@router.get("", response_model=list[ProjectRead])
def list_projects(user: ProjectReader, session: SessionDependency) -> list[Project]:
    statement = select(Project).order_by(Project.created_at)
    allowed = project_ids_for_user(session, user)
    if allowed is not None:
        if not allowed:
            return []
        statement = statement.where(Project.id.in_(allowed))
    return list(session.exec(statement).all())


@router.post("", response_model=ProjectRead, status_code=status.HTTP_201_CREATED)
def create_project(
    payload: ProjectPayload, _: AdminUser, session: SessionDependency
) -> Project:
    project = Project(
        name=payload.name.strip(),
        description=clean_optional(payload.description),
        service_url=clean_optional(payload.service_url),
        agent_context=clean_optional(payload.agent_context),
    )
    session.add(project)
    session.commit()
    session.refresh(project)
    return project


@router.get("/{project_id}", response_model=ProjectRead)
def get_project(
    project_id: UUID, user: ProjectReader, session: SessionDependency
) -> Project:
    return require_project_access(session, user, project_id)


@router.patch("/{project_id}", response_model=ProjectRead)
def update_project(
    project_id: UUID,
    payload: ProjectUpdate,
    _: AdminUser,
    session: SessionDependency,
) -> Project:
    project = session.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="项目不存在")
    changes = payload.model_dump(exclude_unset=True)
    if "name" in changes:
        changes["name"] = changes["name"].strip()
    for field in ("description", "service_url", "agent_context"):
        if field in changes:
            changes[field] = clean_optional(changes[field])
    for key, value in changes.items():
        setattr(project, key, value)
    project.updated_at = datetime.now(UTC)
    session.add(project)
    session.commit()
    session.refresh(project)
    return project


@router.get("/{project_id}/members", response_model=list[UUID])
def list_project_members(
    project_id: UUID, _: AdminUser, session: SessionDependency
) -> list[UUID]:
    if session.get(Project, project_id) is None:
        raise HTTPException(status_code=404, detail="项目不存在")
    return list(
        session.exec(
            select(ProjectMember.user_id).where(ProjectMember.project_id == project_id)
        ).all()
    )


@router.put("/{project_id}/members", response_model=list[UUID])
def replace_project_members(
    project_id: UUID,
    payload: ProjectMembersUpdate,
    _: AdminUser,
    session: SessionDependency,
) -> list[UUID]:
    if session.get(Project, project_id) is None:
        raise HTTPException(status_code=404, detail="项目不存在")
    unique_ids = list(dict.fromkeys(payload.user_ids))
    if unique_ids:
        users = session.exec(
            select(User).where(
                User.id.in_(unique_ids),
                User.system_role == SystemRole.USER,
                User.is_active == True,  # noqa: E712
            )
        ).all()
        if {user.id for user in users} != set(unique_ids):
            raise HTTPException(status_code=422, detail="成员中包含不存在或不可用的普通用户")
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
