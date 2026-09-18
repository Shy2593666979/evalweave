from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, status
from sqlmodel import Session

from evalweave.api.response import APIResponse
from evalweave.api.schemas.projects import (
    ProjectMembersUpdate,
    ProjectPayload,
    ProjectRead,
    ProjectUpdate,
)
from evalweave.auth.dependencies import AdminUser, require_permission
from evalweave.auth.permissions import Permission
from evalweave.db.models import Project, User
from evalweave.db.session import get_session
from evalweave.services.projects import ProjectService

router = APIRouter(prefix="/projects", tags=["projects"])

SessionDependency = Annotated[Session, Depends(get_session)]
ProjectReader = Annotated[User, Depends(require_permission(Permission.PROJECT_READ))]


@router.get("", response_model=APIResponse[list[ProjectRead]])
def list_projects(user: ProjectReader, session: SessionDependency) -> APIResponse[list[Project]]:
    return APIResponse.success(ProjectService.list(session, user))


@router.post("", response_model=APIResponse[ProjectRead], status_code=status.HTTP_201_CREATED)
def create_project(
    payload: ProjectPayload, _: AdminUser, session: SessionDependency
) -> APIResponse[Project]:
    return APIResponse.success(ProjectService.create(session, payload.model_dump()))


@router.get("/{project_id}", response_model=APIResponse[ProjectRead])
def get_project(
    project_id: UUID, user: ProjectReader, session: SessionDependency
) -> APIResponse[Project]:
    return APIResponse.success(ProjectService.require_access(session, user, project_id))


@router.patch("/{project_id}", response_model=APIResponse[ProjectRead])
def update_project(
    project_id: UUID,
    payload: ProjectUpdate,
    _: AdminUser,
    session: SessionDependency,
) -> APIResponse[Project]:
    return APIResponse.success(
        ProjectService.update(
            session,
            project_id,
            payload.model_dump(exclude_unset=True),
        )
    )


@router.get("/{project_id}/members", response_model=APIResponse[list[UUID]])
def list_project_members(
    project_id: UUID, _: AdminUser, session: SessionDependency
) -> APIResponse[list[UUID]]:
    return APIResponse.success(ProjectService.list_members(session, project_id))


@router.put("/{project_id}/members", response_model=APIResponse[list[UUID]])
def replace_project_members(
    project_id: UUID,
    payload: ProjectMembersUpdate,
    _: AdminUser,
    session: SessionDependency,
) -> APIResponse[list[UUID]]:
    return APIResponse.success(
        ProjectService.replace_members(session, project_id, payload.user_ids)
    )
