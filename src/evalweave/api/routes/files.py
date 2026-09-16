from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, File, Form, HTTPException, Response, UploadFile, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict
from sqlalchemy.exc import IntegrityError
from sqlmodel import select

from evalweave.auth.dependencies import SessionDependency, require_permission
from evalweave.auth.permissions import Permission
from evalweave.core.config import get_settings
from evalweave.db.models import FileObject, SystemRole, User
from evalweave.project_access import require_project_access
from evalweave.storage import FileTooLargeError, LocalFileStorage

router = APIRouter(tags=["files"])


class FileCategory(StrEnum):
    DATASET_SOURCE = "dataset_source"
    GENERATED_SCRIPT = "generated_script"
    EXECUTION_LOG = "execution_log"
    CONVERSATION_TRACE = "conversation_trace"
    EVALUATION_RESULT = "evaluation_result"
    REPORT = "report"
    SKILL_PACKAGE = "skill_package"


class FileRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    project_id: UUID
    created_by: UUID
    category: str
    original_name: str
    content_type: str | None
    size_bytes: int
    sha256: str
    created_at: datetime


ProjectReader = Annotated[User, Depends(require_permission(Permission.PROJECT_READ))]
DatasetWriter = Annotated[User, Depends(require_permission(Permission.DATASET_WRITE))]
UploadedFile = Annotated[UploadFile, File()]
FileCategoryForm = Annotated[FileCategory, Form()]


def get_file_storage() -> LocalFileStorage:
    return LocalFileStorage(get_settings().storage.local_directory)


def require_file(file_id: UUID, session: SessionDependency) -> FileObject:
    file_object = session.get(FileObject, file_id)
    if file_object is None:
        raise HTTPException(status_code=404, detail="File not found")
    return file_object


def safe_filename(filename: str | None) -> str:
    name = Path((filename or "upload").replace("\\", "/")).name.strip()
    if not name or name in {".", ".."}:
        return "upload"
    return name[:255]


@router.post(
    "/projects/{project_id}/files",
    response_model=FileRead,
    status_code=status.HTTP_201_CREATED,
)
def upload_file(
    project_id: UUID,
    user: DatasetWriter,
    session: SessionDependency,
    file: UploadedFile,
    category: FileCategoryForm = FileCategory.DATASET_SOURCE,
) -> FileObject:
    require_project_access(session, user, project_id)
    settings = get_settings()
    original_name = safe_filename(file.filename)
    if category == FileCategory.DATASET_SOURCE:
        extension = Path(original_name).suffix.lower().lstrip(".")
        allowed = {item.lower().lstrip(".") for item in settings.evaluation.allowed_extensions}
        if extension not in allowed:
            raise HTTPException(
                status_code=422,
                detail=f"Unsupported dataset file extension: {extension or '(none)'}",
            )

    file_id = uuid4()
    storage_key = f"projects/{project_id}/{category.value}/{file_id.hex}"
    storage = get_file_storage()
    try:
        stored = storage.put(
            storage_key,
            file.file,
            max_bytes=settings.evaluation.max_file_size_mb * 1024 * 1024,
        )
    except FileTooLargeError as error:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"File exceeds {settings.evaluation.max_file_size_mb} MB limit",
        ) from error
    finally:
        file.file.close()

    file_object = FileObject(
        id=file_id,
        project_id=project_id,
        created_by=user.id,
        category=category.value,
        original_name=original_name,
        storage_key=storage_key,
        content_type=file.content_type,
        size_bytes=stored.size_bytes,
        sha256=stored.sha256,
    )
    session.add(file_object)
    try:
        session.commit()
    except IntegrityError as error:
        session.rollback()
        storage.delete(storage_key)
        raise HTTPException(status_code=409, detail="File metadata conflict") from error
    except Exception:
        session.rollback()
        storage.delete(storage_key)
        raise
    session.refresh(file_object)
    return file_object


@router.get("/projects/{project_id}/files", response_model=list[FileRead])
def list_files(
    project_id: UUID,
    user: ProjectReader,
    session: SessionDependency,
    category: FileCategory | None = None,
) -> list[FileObject]:
    require_project_access(session, user, project_id)
    statement = select(FileObject).where(FileObject.project_id == project_id)
    if user.system_role != SystemRole.ADMIN:
        statement = statement.where(
            (FileObject.category == FileCategory.DATASET_SOURCE.value)
            | (FileObject.created_by == user.id)
        )
    if category is not None:
        statement = statement.where(FileObject.category == category.value)
    return list(session.exec(statement.order_by(FileObject.created_at.desc())).all())


@router.get("/files/{file_id}", response_model=FileRead)
def get_file_metadata(
    file_id: UUID,
    user: ProjectReader,
    session: SessionDependency,
) -> FileObject:
    file_object = require_file(file_id, session)
    require_project_access(session, user, file_object.project_id)
    require_file_access(file_object, user)
    return file_object


def require_file_access(file_object: FileObject, user: User) -> None:
    if (
        user.system_role != SystemRole.ADMIN
        and file_object.category != FileCategory.DATASET_SOURCE.value
        and file_object.created_by != user.id
    ):
        raise HTTPException(status_code=404, detail="文件不存在")


@router.get("/files/{file_id}/content", response_class=FileResponse)
def download_file(
    file_id: UUID,
    user: ProjectReader,
    session: SessionDependency,
) -> FileResponse:
    file_object = require_file(file_id, session)
    require_project_access(session, user, file_object.project_id)
    require_file_access(file_object, user)
    path = get_file_storage().path_for(file_object.storage_key)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Stored file content not found")
    return FileResponse(
        path=path,
        filename=file_object.original_name,
        media_type=file_object.content_type or "application/octet-stream",
    )


@router.delete("/files/{file_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_file(
    file_id: UUID,
    user: DatasetWriter,
    session: SessionDependency,
) -> Response:
    file_object = require_file(file_id, session)
    require_project_access(session, user, file_object.project_id)
    require_file_access(file_object, user)
    get_file_storage().delete(file_object.storage_key)
    session.delete(file_object)
    session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
