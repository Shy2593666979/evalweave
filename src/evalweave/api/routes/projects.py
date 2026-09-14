from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlmodel import Session, SQLModel

from evalweave.auth.dependencies import require_permission
from evalweave.auth.permissions import Permission
from evalweave.db.models import Project
from evalweave.db.session import get_session
from evalweave.workspace import ensure_single_workspace

router = APIRouter(prefix="/projects", tags=["projects"])


SessionDependency = Annotated[Session, Depends(get_session)]
ProjectReader = Annotated[object, Depends(require_permission(Permission.PROJECT_READ))]
ProjectWriter = Annotated[object, Depends(require_permission(Permission.PROJECT_WRITE))]


class ProjectCreate(SQLModel):
    name: str
    description: str | None = None


@router.get("", response_model=list[Project])
def list_projects(_: ProjectReader, session: SessionDependency) -> list[Project]:
    return [ensure_single_workspace(session)]


@router.post("", response_model=Project, status_code=status.HTTP_201_CREATED)
def create_project(payload: ProjectCreate, _: ProjectWriter, session: SessionDependency) -> Project:
    # Compatibility endpoint for older clients. The product now has one workspace,
    # so this updates and returns it instead of creating another partition.
    project = ensure_single_workspace(session)
    project.name = payload.name.strip() or project.name
    project.description = payload.description
    session.add(project)
    session.commit()
    session.refresh(project)
    return project


@router.get("/{project_id}", response_model=Project)
def get_project(project_id: UUID, _: ProjectReader, session: SessionDependency) -> Project:
    project = session.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    return project
