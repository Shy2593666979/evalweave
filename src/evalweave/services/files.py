from __future__ import annotations

from pathlib import Path
from typing import BinaryIO
from uuid import UUID, uuid4

from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from evalweave.core.config import Settings
from evalweave.db.models import FileObject, SystemRole, User
from evalweave.services.exceptions import (
    ConflictError,
    NotFoundError,
    PayloadTooLargeError,
    ValidationError,
)
from evalweave.services.projects import ProjectService
from evalweave.storage import FileTooLargeError, LocalFileStorage


def safe_filename(filename: str | None) -> str:
    name = Path((filename or "upload").replace("\\", "/")).name.strip()
    if not name or name in {".", ".."}:
        return "upload"
    return name[:255]


class FileService:
    @staticmethod
    def require(session: Session, file_id: UUID) -> FileObject:
        file_object = session.get(FileObject, file_id)
        if file_object is None:
            raise NotFoundError("文件不存在")
        return file_object

    @staticmethod
    def require_access(file_object: FileObject, user: User) -> None:
        if (
            user.system_role != SystemRole.ADMIN
            and file_object.category != "dataset_source"
            and file_object.created_by != user.id
        ):
            raise NotFoundError("文件不存在")

    @staticmethod
    def upload(
        session: Session,
        storage: LocalFileStorage,
        settings: Settings,
        project_id: UUID,
        user: User,
        stream: BinaryIO,
        *,
        filename: str | None,
        content_type: str | None,
        category: str,
    ) -> FileObject:
        ProjectService.require_access(session, user, project_id)
        original_name = safe_filename(filename)
        if category == "dataset_source":
            extension = Path(original_name).suffix.lower().lstrip(".")
            allowed = {
                item.lower().lstrip(".")
                for item in settings.evaluation.allowed_extensions
            }
            if extension not in allowed:
                raise ValidationError(
                    f"不支持的数据文件扩展名：{extension or '无扩展名'}"
                )
        file_id = uuid4()
        storage_key = f"projects/{project_id}/{category}/{file_id.hex}"
        try:
            stored = storage.put(
                storage_key,
                stream,
                max_bytes=settings.evaluation.max_file_size_mb * 1024 * 1024,
            )
        except FileTooLargeError as error:
            raise PayloadTooLargeError(
                f"文件超过 {settings.evaluation.max_file_size_mb} MB 限制"
            ) from error
        finally:
            stream.close()
        file_object = FileObject(
            id=file_id,
            project_id=project_id,
            created_by=user.id,
            category=category,
            original_name=original_name,
            storage_key=storage_key,
            content_type=content_type,
            size_bytes=stored.size_bytes,
            sha256=stored.sha256,
        )
        session.add(file_object)
        try:
            session.commit()
        except IntegrityError as error:
            session.rollback()
            storage.delete(storage_key)
            raise ConflictError("文件元数据冲突") from error
        except Exception:
            session.rollback()
            storage.delete(storage_key)
            raise
        session.refresh(file_object)
        return file_object

    @staticmethod
    def list(
        session: Session,
        project_id: UUID,
        user: User,
        category: str | None,
    ) -> list[FileObject]:
        ProjectService.require_access(session, user, project_id)
        statement = select(FileObject).where(FileObject.project_id == project_id)
        if user.system_role != SystemRole.ADMIN:
            statement = statement.where(
                (FileObject.category == "dataset_source")
                | (FileObject.created_by == user.id)
            )
        if category is not None:
            statement = statement.where(FileObject.category == category)
        return list(session.exec(statement.order_by(FileObject.created_at.desc())).all())

    @staticmethod
    def get_for_user(session: Session, file_id: UUID, user: User) -> FileObject:
        file_object = FileService.require(session, file_id)
        ProjectService.require_access(session, user, file_object.project_id)
        FileService.require_access(file_object, user)
        return file_object

    @staticmethod
    def content_path(
        session: Session,
        storage: LocalFileStorage,
        file_id: UUID,
        user: User,
    ) -> tuple[FileObject, Path]:
        file_object = FileService.get_for_user(session, file_id, user)
        path = storage.path_for(file_object.storage_key)
        if not path.is_file():
            raise NotFoundError("文件内容不存在")
        return file_object, path

    @staticmethod
    def delete(
        session: Session,
        storage: LocalFileStorage,
        file_id: UUID,
        user: User,
    ) -> None:
        file_object = FileService.get_for_user(session, file_id, user)
        storage.delete(file_object.storage_key)
        session.delete(file_object)
        session.commit()
