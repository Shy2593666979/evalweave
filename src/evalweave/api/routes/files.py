from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, Response, UploadFile, status
from fastapi.responses import FileResponse

from evalweave.api.response import APIResponse
from evalweave.api.schemas.files import FileCategory, FileRead
from evalweave.auth.dependencies import SessionDependency, require_permission
from evalweave.auth.permissions import Permission
from evalweave.core.config import get_settings
from evalweave.db.models import FileObject, User
from evalweave.services.files import FileService
from evalweave.storage import LocalFileStorage

router = APIRouter(tags=["files"])


ProjectReader = Annotated[User, Depends(require_permission(Permission.PROJECT_READ))]
DatasetWriter = Annotated[User, Depends(require_permission(Permission.DATASET_WRITE))]
UploadedFile = Annotated[UploadFile, File()]
FileCategoryForm = Annotated[FileCategory, Form()]


def get_file_storage() -> LocalFileStorage:
    return LocalFileStorage(get_settings().storage.local_directory)


def require_file(file_id: UUID, session: SessionDependency) -> FileObject:
    return FileService.require(session, file_id)


@router.post(
    "/projects/{project_id}/files",
    response_model=APIResponse[FileRead],
    status_code=status.HTTP_201_CREATED,
)
def upload_file(
    project_id: UUID,
    user: DatasetWriter,
    session: SessionDependency,
    file: UploadedFile,
    category: FileCategoryForm = FileCategory.DATASET_SOURCE,
) -> APIResponse[FileObject]:
    return APIResponse.success(FileService.upload(
        session,
        get_file_storage(),
        get_settings(),
        project_id,
        user,
        file.file,
        filename=file.filename,
        content_type=file.content_type,
        category=category.value,
    ))


@router.get("/projects/{project_id}/files", response_model=APIResponse[list[FileRead]])
def list_files(
    project_id: UUID,
    user: ProjectReader,
    session: SessionDependency,
    category: FileCategory | None = None,
) -> APIResponse[list[FileObject]]:
    return APIResponse.success(FileService.list(
        session,
        project_id,
        user,
        category.value if category else None,
    ))


@router.get("/files/{file_id}", response_model=APIResponse[FileRead])
def get_file_metadata(
    file_id: UUID,
    user: ProjectReader,
    session: SessionDependency,
) -> APIResponse[FileObject]:
    return APIResponse.success(FileService.get_for_user(session, file_id, user))


@router.get("/files/{file_id}/content", response_class=FileResponse)
def download_file(
    file_id: UUID,
    user: ProjectReader,
    session: SessionDependency,
) -> FileResponse:
    file_object, path = FileService.content_path(
        session,
        get_file_storage(),
        file_id,
        user,
    )
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
    FileService.delete(session, get_file_storage(), file_id, user)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
