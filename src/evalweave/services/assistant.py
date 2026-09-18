from __future__ import annotations

import re
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from logging import getLogger
from queue import Empty, Full, Queue
from threading import Thread
from typing import Any
from uuid import UUID

from sqlalchemy.engine import Engine
from sqlmodel import Session, select

from evalweave.agents.model_client import AgentModelClient
from evalweave.agents.model_config import resolve_agent_config
from evalweave.agents.prompts import CONVERSATION_TITLE_PROMPT
from evalweave.auth.permissions import Permission
from evalweave.core.config import AgentConfig
from evalweave.db.models import (
    AgentJob,
    AssistantConversation,
    AssistantMessage,
    FileObject,
    Project,
    SystemRole,
    User,
    UserType,
)
from evalweave.services.agent_jobs import AgentJobService
from evalweave.services.exceptions import ConflictError, NotFoundError, ValidationError
from evalweave.services.projects import ProjectService

DEFAULT_CONVERSATION_TITLE = "新的评测对话"
logger = getLogger(__name__)
WELCOME_MESSAGE = (
    "你好，我是评测助手。告诉我你要评测什么：可以上传数据文件，也可以直接说明"
    "接口地址、调用方式和判断标准。我会边聊边整理成可执行任务。"
)


class AssistantStreamPersister:
    """Persist the newest live reply snapshot without blocking model streaming."""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self._queue: Queue[tuple[UUID, str] | None] = Queue(maxsize=1)
        self._closed = False
        self._thread = Thread(
            target=self._run,
            name="assistant-stream-persistence",
            daemon=True,
        )
        self._thread.start()

    def submit(self, message_id: UUID, content: str) -> None:
        """Queue only the latest snapshot when database writes fall behind."""
        if self._closed:
            return
        snapshot = (message_id, content)
        try:
            self._queue.put_nowait(snapshot)
            return
        except Full:
            pass
        try:
            self._queue.get_nowait()
            self._queue.task_done()
        except Empty:
            pass
        with suppress(Full):
            self._queue.put_nowait(snapshot)

    def flush(self) -> None:
        self._queue.join()

    def close(self) -> None:
        if self._closed:
            return
        self.flush()
        self._closed = True
        self._queue.put(None)
        self._thread.join()

    def _run(self) -> None:
        while True:
            snapshot = self._queue.get()
            try:
                if snapshot is None:
                    return
                message_id, content = snapshot
                AssistantService.persist_streaming_message(
                    self.engine,
                    message_id,
                    content,
                    is_streaming=True,
                    include_in_context=False,
                )
            except Exception:
                logger.exception("Failed to persist an assistant stream snapshot")
            finally:
                self._queue.task_done()


def normalize_conversation_title(value: str, question: str) -> str:
    candidate = (value.splitlines()[0] if value.strip() else "").strip()
    candidate = re.sub(r"^(?:标题|对话名称)\s*[：:]\s*", "", candidate)
    candidate = candidate.strip("`*_# \"'“”‘’《》<>，。！？；：,.!?;:-")
    candidate = re.sub(r"\s+", "", candidate)
    if len(candidate) < 2:
        candidate = re.sub(r"\s+", "", question).strip("`*_# \"'“”‘’《》<>，。！？；：,.!?;:-")
    candidate = candidate[:10]
    return candidate if len(candidate) >= 2 else "新对话"


@dataclass(frozen=True)
class AssistantTurn:
    conversation_id: UUID
    project_id: UUID | None
    owner_id: UUID
    current_draft: dict[str, Any]
    message_history: list[dict[str, str]]
    assistant_context: dict[str, Any]
    initial_message_id: UUID
    should_generate_title: bool


class AssistantService:
    @staticmethod
    def generate_conversation_title(
        engine: Engine,
        conversation_id: UUID,
        owner_id: UUID,
        question: str,
        evaluation_model_id: UUID | None,
    ) -> None:
        generated = ""
        try:
            with Session(engine) as session:
                config = resolve_agent_config(session, evaluation_model_id)
            if config.enabled and config.base_url and config.model:
                generated = AgentModelClient(config).request_text(
                    CONVERSATION_TITLE_PROMPT,
                    [{"role": "user", "content": question}],
                )
        except Exception:
            generated = ""
        title = normalize_conversation_title(generated, question)
        try:
            with Session(engine) as session:
                conversation = session.get(AssistantConversation, conversation_id)
                if (
                    conversation is None
                    or conversation.created_by != owner_id
                    or conversation.title != DEFAULT_CONVERSATION_TITLE
                ):
                    return
                conversation.title = title
                conversation.updated_at = datetime.now(UTC)
                session.add(conversation)
                session.commit()
        except Exception:
            return

    @staticmethod
    def require_conversation(
        session: Session,
        conversation_id: UUID,
        user: User,
    ) -> AssistantConversation:
        conversation = session.get(AssistantConversation, conversation_id)
        if conversation is None or conversation.created_by != user.id:
            raise NotFoundError("评测助手对话不存在")
        if conversation.project_id:
            ProjectService.require_access(session, user, conversation.project_id)
        return conversation

    @staticmethod
    def list_conversations(
        session: Session,
        user: User,
        project_id: UUID | None = None,
    ) -> list[AssistantConversation]:
        statement = (
            select(AssistantConversation)
            .where(AssistantConversation.created_by == user.id)
            .order_by(AssistantConversation.updated_at.desc())
        )
        if project_id is not None:
            ProjectService.require_access(session, user, project_id)
            statement = statement.where(AssistantConversation.project_id == project_id)
        else:
            allowed = ProjectService.accessible_ids(session, user)
            if allowed is not None:
                if not allowed:
                    return []
                statement = statement.where(AssistantConversation.project_id.in_(allowed))
        return list(session.exec(statement).all())

    @staticmethod
    def create_conversation(
        session: Session,
        user: User,
        project_id: UUID | None,
    ) -> AssistantConversation:
        if project_id:
            ProjectService.require_access(session, user, project_id)
        conversation = AssistantConversation(project_id=project_id, created_by=user.id)
        session.add(conversation)
        session.commit()
        session.refresh(conversation)
        session.add(
            AssistantMessage(
                conversation_id=conversation.id,
                role="assistant",
                content=WELCOME_MESSAGE,
            )
        )
        session.commit()
        return conversation

    @staticmethod
    def update_conversation(
        session: Session,
        conversation_id: UUID,
        user: User,
        title: str,
    ) -> AssistantConversation:
        conversation = AssistantService.require_conversation(session, conversation_id, user)
        conversation.title = title.strip()
        conversation.updated_at = datetime.now(UTC)
        session.add(conversation)
        session.commit()
        session.refresh(conversation)
        return conversation

    @staticmethod
    def delete_conversation(
        session: Session,
        conversation_id: UUID,
        user: User,
    ) -> None:
        conversation = AssistantService.require_conversation(session, conversation_id, user)
        messages = list(
            session.exec(
                select(AssistantMessage).where(AssistantMessage.conversation_id == conversation.id)
            ).all()
        )
        if any(message.is_streaming for message in messages):
            raise ConflictError("正在回复的对话暂时不能删除")
        for message in messages:
            session.delete(message)
        session.delete(conversation)
        session.commit()

    @staticmethod
    def list_messages(
        session: Session,
        conversation_id: UUID,
        user: User,
    ) -> list[AssistantMessage]:
        AssistantService.require_conversation(session, conversation_id, user)
        statement = (
            select(AssistantMessage)
            .where(AssistantMessage.conversation_id == conversation_id)
            .order_by(AssistantMessage.created_at)
        )
        return list(session.exec(statement).all())

    @staticmethod
    def mark_started(
        session: Session,
        conversation_id: UUID,
        job_id: UUID,
        user: User,
    ) -> AssistantConversation:
        conversation = AssistantService.require_conversation(session, conversation_id, user)
        job: AgentJob = AgentJobService.require(session, job_id)
        if job.created_by != user.id or job.project_id != conversation.project_id:
            raise ValidationError("评测任务与当前对话不匹配")
        conversation.agent_job_id = job.id
        conversation.status = "started"
        conversation.updated_at = datetime.now(UTC)
        session.add(conversation)
        session.commit()
        session.refresh(conversation)
        return conversation

    @staticmethod
    def prepare_turn(
        session: Session,
        conversation: AssistantConversation,
        user: User,
        *,
        content: str,
        source_file_id: UUID | None,
        evaluation_model_id: UUID | None,
        explicit_output_format: str | None,
        config: AgentConfig,
        initial_message_id: UUID,
    ) -> AssistantTurn:
        source: FileObject | None = None
        if source_file_id:
            source = session.get(FileObject, source_file_id)
            if source is None or source.project_id != conversation.project_id:
                raise ValidationError("数据文件不属于当前项目")
        has_user_message = (
            session.exec(
                select(AssistantMessage).where(
                    AssistantMessage.conversation_id == conversation.id,
                    AssistantMessage.role == "user",
                )
            ).first()
            is not None
        )
        should_generate_title = (
            conversation.title == DEFAULT_CONVERSATION_TITLE and not has_user_message
        )
        session.add(
            AssistantMessage(
                conversation_id=conversation.id,
                role="user",
                content=content,
                attachment_file_id=source.id if source else None,
                attachment_name=source.original_name if source else None,
                attachment_content_type=source.content_type if source else None,
                attachment_size_bytes=source.size_bytes if source else None,
            )
        )
        draft = dict(conversation.draft)
        if source:
            draft["source_file_id"] = str(source.id)
        if explicit_output_format:
            draft["output_format"] = explicit_output_format
        if evaluation_model_id:
            draft["evaluation_model_id"] = str(evaluation_model_id)
        conversation.draft = draft
        conversation.updated_at = datetime.now(UTC)
        session.add(conversation)
        session.commit()

        statement = (
            select(AssistantMessage)
            .where(
                AssistantMessage.conversation_id == conversation.id,
                (AssistantMessage.role == "user") | (AssistantMessage.include_in_context == True),  # noqa: E712
            )
            .order_by(AssistantMessage.created_at)
        )
        message_history = [
            {"role": item.role, "content": item.content} for item in session.exec(statement).all()
        ]
        if message_history:
            message_history[-1]["content"] = content

        project_id = conversation.project_id
        project = session.get(Project, project_id) if project_id else None
        user_types = {item.id: item for item in session.exec(select(UserType)).all()}
        reviewer_type_counts = {item.code.lower(): 0 for item in user_types.values()}
        available_reviewer_usernames: list[str] = []
        candidates = session.exec(select(User).where(User.is_active == True)).all()  # noqa: E712
        for candidate in candidates:
            candidate_type = user_types.get(candidate.user_type_id)
            can_review = candidate.system_role == SystemRole.ADMIN or bool(
                candidate_type and Permission.EVALUATION_REVIEW.value in candidate_type.permissions
            )
            if not can_review:
                continue
            available_reviewer_usernames.append(candidate.username.lower())
            if candidate_type:
                code = candidate_type.code.lower()
                reviewer_type_counts[code] = reviewer_type_counts.get(code, 0) + 1

        session.add(
            AssistantMessage(
                id=initial_message_id,
                conversation_id=conversation.id,
                role="assistant",
                content="",
                include_in_context=False,
                is_streaming=True,
            )
        )
        session.commit()
        assistant_context = {
            "project_id": str(project_id) if project_id else None,
            "project": AssistantService._project_context(project),
            "current_draft": draft,
            "target_auth_configured": bool(config.target_headers or config.target_auth_flows),
            "configured_target_header_names": list(config.target_headers),
            "configured_auth_hosts": list(config.target_auth_flows),
            "available_output_formats": ["xlsx", "jsonl", "markdown", "text"],
            "reviewer_type_counts": reviewer_type_counts,
            "available_reviewer_usernames": available_reviewer_usernames,
        }
        return AssistantTurn(
            conversation_id=conversation.id,
            project_id=project_id,
            owner_id=user.id,
            current_draft=draft,
            message_history=message_history,
            assistant_context=assistant_context,
            initial_message_id=initial_message_id,
            should_generate_title=should_generate_title,
        )

    @staticmethod
    def persist_streaming_message(
        engine: Engine,
        message_id: UUID,
        content: str,
        *,
        is_streaming: bool,
        include_in_context: bool,
        ui_action: dict[str, Any] | None = None,
        attachment: dict[str, Any] | None = None,
    ) -> None:
        with Session(engine) as session:
            message = session.get(AssistantMessage, message_id)
            if message is None:
                return
            has_attachment = bool(attachment and attachment.get("attachment_file_id"))
            if not is_streaming and not content.strip() and not has_attachment:
                session.delete(message)
                session.commit()
                return
            message.content = content
            message.is_streaming = is_streaming
            message.include_in_context = include_in_context
            message.ui_action = ui_action
            if attachment is not None:
                raw_attachment_id = attachment.get("attachment_file_id")
                message.attachment_file_id = (
                    UUID(str(raw_attachment_id)) if raw_attachment_id else None
                )
                message.attachment_name = attachment.get("attachment_name")
                message.attachment_content_type = attachment.get("attachment_content_type")
                message.attachment_size_bytes = attachment.get("attachment_size_bytes")
            session.add(message)
            session.commit()

    @staticmethod
    def advance_streaming_message(
        engine: Engine,
        conversation_id: UUID,
        message_id: UUID,
        content: str,
        next_message_id: UUID,
    ) -> None:
        with Session(engine) as session:
            message = session.get(AssistantMessage, message_id)
            if message is not None:
                if content.strip():
                    message.content = content
                    message.is_streaming = False
                    message.include_in_context = False
                    session.add(message)
                else:
                    session.delete(message)
            session.add(
                AssistantMessage(
                    id=next_message_id,
                    conversation_id=conversation_id,
                    role="assistant",
                    content="",
                    include_in_context=False,
                    is_streaming=True,
                )
            )
            session.commit()

    @staticmethod
    def complete_turn(
        engine: Engine,
        conversation_id: UUID,
        owner_id: UUID,
        draft: dict[str, Any],
        stage: str,
        result: dict[str, Any],
    ) -> dict[str, Any] | None:
        with Session(engine) as session:
            conversation = session.get(AssistantConversation, conversation_id)
            if conversation is None or conversation.created_by != owner_id:
                return None
            output = AssistantService._output_attachment(
                session,
                result,
                conversation.project_id,
            )
            attachment = {
                "attachment_file_id": str(output.id) if output else None,
                "attachment_name": output.original_name if output else None,
                "attachment_content_type": output.content_type if output else None,
                "attachment_size_bytes": output.size_bytes if output else None,
            }
            conversation.draft = draft
            conversation.status = stage
            conversation.updated_at = datetime.now(UTC)
            session.add(conversation)
            session.commit()
            return attachment

    @staticmethod
    def _project_context(project: Project | None) -> dict[str, Any] | None:
        if project is None:
            return None
        return {
            "name": project.name,
            "service_url": project.service_url,
            "description": project.description,
            "agent_context": project.agent_context,
        }

    @staticmethod
    def _output_attachment(
        session: Session,
        result: dict[str, Any],
        project_id: UUID | None,
    ) -> FileObject | None:
        trace = result.get("react_trace")
        if not isinstance(trace, list):
            return None
        for item in reversed(trace):
            if not isinstance(item, dict) or item.get("name") not in {
                "run_python",
                "inspect_agent_job",
            }:
                continue
            if item.get("status") != "completed" or not isinstance(item.get("summary"), dict):
                continue
            summary = item["summary"]
            file_id = summary.get("primary_output_file_id")
            outputs = summary.get("outputs")
            if not file_id and isinstance(outputs, list) and outputs:
                last_output = outputs[-1]
                if isinstance(last_output, dict):
                    file_id = last_output.get("file_id")
            try:
                output = session.get(FileObject, UUID(str(file_id))) if file_id else None
            except ValueError:
                output = None
            if output is not None and (project_id is None or output.project_id == project_id):
                return output
        return None
