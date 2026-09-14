from __future__ import annotations

import json
import re
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlmodel import Session, select

from evalweave.agents.model_config import resolve_agent_config
from evalweave.agents.planner import (
    assist_job_configuration,
    derive_task_title,
)
from evalweave.agents.react_runtime import stream_react_configuration
from evalweave.auth.dependencies import SessionDependency, require_permission
from evalweave.auth.permissions import Permission
from evalweave.core.config import get_settings
from evalweave.db.models import (
    AgentJob,
    AgentJobEvent,
    AgentJobStatus,
    AgentStep,
    AssistantConversation,
    AssistantMessage,
    EvaluationModel,
    FileObject,
    HumanTask,
    HumanTaskStatus,
    NotificationDelivery,
    Project,
    SystemRole,
    User,
    UserType,
)
from evalweave.db.session import get_engine
from evalweave.notifications import notify_human_task
from evalweave.workers.factory import create_celery_app

router = APIRouter(tags=["evaluation-agent"])

ExperimentReader = Annotated[User, Depends(require_permission(Permission.EXPERIMENT_READ))]
ExperimentRunner = Annotated[User, Depends(require_permission(Permission.EXPERIMENT_RUN))]
EvaluationReviewer = Annotated[User, Depends(require_permission(Permission.EVALUATION_REVIEW))]


def redact_sensitive_content(content: str) -> str:
    redacted = re.sub(
        r"(?i)(authorization\s*:\s*bearer\s+)[^\s'\"\\]+",
        r"\1[REDACTED]",
        content,
    )
    redacted = re.sub(
        r"(?is)(\s-b\s+)(['\"]).*?\2",
        r"\1'[REDACTED]'",
        redacted,
    )
    redacted = re.sub(
        r'(?i)("(?:user_)?password"\s*:\s*")[^"]*(")',
        r"\1[REDACTED]\2",
        redacted,
    )
    return redacted


def assistant_output_attachment(
    session: Session,
    result: dict[str, Any],
    project_id: UUID | None,
) -> FileObject | None:
    """Return the newest file produced by a successful tool call in this turn."""
    trace = result.get("react_trace")
    if not isinstance(trace, list):
        return None
    for item in reversed(trace):
        if not isinstance(item, dict) or item.get("name") != "run_python":
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


def enqueue_agent_task(task_name: str, job_id: UUID) -> None:
    create_celery_app().send_task(task_name, args=[str(job_id)])


class NotificationTarget(BaseModel):
    channel: Literal["wecom", "email"]
    recipient: str = Field(min_length=1, max_length=255)


class AgentJobCreate(BaseModel):
    title: str = Field(min_length=1, max_length=128)
    goal: str = Field(min_length=1, max_length=10000)
    source_file_id: UUID | None = None
    input_config: dict[str, Any] = Field(default_factory=dict)
    notification_targets: list[NotificationTarget] = Field(default_factory=list)
    requires_approval: bool | None = None
    output_format: Literal["xlsx", "jsonl", "markdown", "text"] = "xlsx"
    evaluation_model_id: UUID | None = None


class AgentJobRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    project_id: UUID
    created_by: UUID
    source_file_id: UUID | None
    result_file_id: UUID | None
    title: str
    goal: str
    status: AgentJobStatus
    input_config: dict[str, Any]
    eval_spec: dict[str, Any]
    result: dict[str, Any]
    error: str | None
    repair_attempts: int
    max_repair_attempts: int
    requires_approval: bool
    created_at: datetime
    updated_at: datetime


class AgentStepRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    status: str
    attempt: int
    input_data: dict[str, Any]
    output_data: dict[str, Any]
    error: str | None
    started_at: datetime | None
    finished_at: datetime | None


class AgentJobEventRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    job_id: UUID
    phase: str
    event_type: str
    content: str
    payload: dict[str, Any]
    created_at: datetime


class HumanTaskRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    job_id: UUID
    title: str
    instructions: str
    status: HumanTaskStatus
    notification_targets: list[dict[str, str]]
    decision_reason: str | None
    resolved_by: UUID | None
    resolved_at: datetime | None
    created_at: datetime


class HumanDecision(BaseModel):
    decision: Literal["approve", "reject"]
    reason: str | None = Field(default=None, max_length=4000)


class AgentRuntimeRead(BaseModel):
    enabled: bool
    model: str | None
    require_approval: bool
    worker_available: bool


class AgentAssistMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=10000)


class AgentAssistRequest(BaseModel):
    messages: list[AgentAssistMessage] = Field(min_length=1, max_length=20)
    project_id: UUID | None = None
    evaluation_model_id: UUID | None = None


class AgentAssistRead(BaseModel):
    reply: str
    draft: dict[str, Any]


class AssistantConversationCreate(BaseModel):
    project_id: UUID | None = None


class AssistantConversationRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    project_id: UUID | None
    title: str
    draft: dict[str, Any]
    status: str
    agent_job_id: UUID | None
    created_at: datetime
    updated_at: datetime


class AssistantMessageRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    conversation_id: UUID
    role: str
    content: str
    attachment_file_id: UUID | None
    attachment_name: str | None
    attachment_content_type: str | None
    attachment_size_bytes: int | None
    created_at: datetime


class AssistantStreamRequest(BaseModel):
    content: str = Field(min_length=1, max_length=100000)
    source_file_id: UUID | None = None
    evaluation_model_id: UUID | None = None
    output_format: Literal["xlsx", "jsonl", "markdown", "text"] | None = None


class AssistantStartedRequest(BaseModel):
    agent_job_id: UUID


class EvaluationModelOption(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    model_name: str
    api_mode: str


def require_project(project_id: UUID, session: SessionDependency) -> Project:
    project = session.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    return project


def require_job(job_id: UUID, session: SessionDependency) -> AgentJob:
    job = session.get(AgentJob, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Agent job not found")
    return job


def require_human_task(task_id: UUID, session: SessionDependency) -> HumanTask:
    task = session.get(HumanTask, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Human task not found")
    return task


def require_assistant_conversation(
    conversation_id: UUID, user: User, session: SessionDependency
) -> AssistantConversation:
    conversation = session.get(AssistantConversation, conversation_id)
    if conversation is None or conversation.created_by != user.id:
        raise HTTPException(status_code=404, detail="评测助手对话不存在")
    return conversation


@router.get("/agent/runtime", response_model=AgentRuntimeRead)
def get_agent_runtime(_: ExperimentReader) -> AgentRuntimeRead:
    config = get_settings().agent
    try:
        # A Celery broadcast ping commonly takes slightly over 500 ms on Windows,
        # especially with the solo pool. A 0.5 s window caused healthy workers to
        # be reported as disconnected during normal startup and under light load.
        worker_available = bool(create_celery_app().control.inspect(timeout=1.5).ping())
    except Exception:
        worker_available = False
    return AgentRuntimeRead(
        enabled=config.enabled,
        model=config.model or None,
        require_approval=config.require_approval,
        worker_available=worker_available,
    )


@router.get("/evaluation-models", response_model=list[EvaluationModelOption])
def list_available_evaluation_models(
    _: ExperimentReader, session: SessionDependency
) -> list[EvaluationModel]:
    statement = (
        select(EvaluationModel).where(EvaluationModel.is_active).order_by(EvaluationModel.name)
    )
    return list(session.exec(statement).all())


@router.post("/agent/assist", response_model=AgentAssistRead)
def assist_with_agent(
    payload: AgentAssistRequest, _: ExperimentRunner, session: SessionDependency
) -> AgentAssistRead:
    config = resolve_agent_config(session, payload.evaluation_model_id)
    result = assist_job_configuration(
        config,
        [message.model_dump() for message in payload.messages],
        {
            "project_id": str(payload.project_id) if payload.project_id else None,
            "available_output_formats": ["xlsx", "jsonl", "markdown", "text"],
        },
    )
    return AgentAssistRead.model_validate(result)


@router.get("/assistant/conversations", response_model=list[AssistantConversationRead])
def list_assistant_conversations(
    user: ExperimentRunner,
    session: SessionDependency,
) -> list[AssistantConversation]:
    statement = (
        select(AssistantConversation)
        .where(AssistantConversation.created_by == user.id)
        .order_by(AssistantConversation.updated_at.desc())
    )
    return list(session.exec(statement).all())


@router.post(
    "/assistant/conversations",
    response_model=AssistantConversationRead,
    status_code=status.HTTP_201_CREATED,
)
def create_assistant_conversation(
    payload: AssistantConversationCreate,
    user: ExperimentRunner,
    session: SessionDependency,
) -> AssistantConversation:
    if payload.project_id:
        require_project(payload.project_id, session)
    conversation = AssistantConversation(
        project_id=payload.project_id,
        created_by=user.id,
    )
    session.add(conversation)
    session.commit()
    session.refresh(conversation)
    session.add(
        AssistantMessage(
            conversation_id=conversation.id,
            role="assistant",
            content=(
                "你好，我是评测助手。告诉我你要评测什么：可以上传数据文件，也可以直接说明"
                "接口地址、调用方式和判断标准。我会边聊边整理成可执行任务。"
            ),
        )
    )
    session.commit()
    return conversation


@router.get(
    "/assistant/conversations/{conversation_id}/messages",
    response_model=list[AssistantMessageRead],
)
def list_assistant_messages(
    conversation_id: UUID,
    user: ExperimentRunner,
    session: SessionDependency,
) -> list[AssistantMessage]:
    require_assistant_conversation(conversation_id, user, session)
    statement = (
        select(AssistantMessage)
        .where(AssistantMessage.conversation_id == conversation_id)
        .order_by(AssistantMessage.created_at)
    )
    return list(session.exec(statement).all())


@router.post("/assistant/conversations/{conversation_id}/messages/stream")
def stream_assistant_message(
    conversation_id: UUID,
    payload: AssistantStreamRequest,
    user: ExperimentRunner,
    session: SessionDependency,
) -> StreamingResponse:
    conversation = require_assistant_conversation(conversation_id, user, session)
    source: FileObject | None = None
    if payload.source_file_id:
        source = session.get(FileObject, payload.source_file_id)
        if source is None or source.project_id != conversation.project_id:
            raise HTTPException(status_code=422, detail="数据文件不属于当前项目")
    user_message = AssistantMessage(
        conversation_id=conversation.id,
        role="user",
        content=redact_sensitive_content(payload.content.strip()),
        attachment_file_id=source.id if source else None,
        attachment_name=source.original_name if source else None,
        attachment_content_type=source.content_type if source else None,
        attachment_size_bytes=source.size_bytes if source else None,
    )
    session.add(user_message)
    if conversation.title == "新的评测对话":
        conversation.title = payload.content.strip()[:36]
    if source:
        conversation.draft = {
            **conversation.draft,
            "source_file_id": str(source.id),
        }
    if payload.output_format:
        conversation.draft = {
            **conversation.draft,
            "output_format": payload.output_format,
        }
    conversation.updated_at = datetime.now(UTC)
    session.add(conversation)
    session.commit()

    statement = (
        select(AssistantMessage)
        .where(AssistantMessage.conversation_id == conversation.id)
        .order_by(AssistantMessage.created_at)
    )
    message_history = [
        {"role": item.role, "content": item.content} for item in session.exec(statement).all()
    ]
    if message_history:
        message_history[-1]["content"] = payload.content.strip()
    current_draft = dict(conversation.draft)
    project_id = conversation.project_id
    owner_id = user.id
    config = resolve_agent_config(session, payload.evaluation_model_id)
    user_types = {item.id: item for item in session.exec(select(UserType)).all()}
    reviewer_type_counts: dict[str, int] = {
        item.code.lower(): 0 for item in user_types.values()
    }
    available_reviewer_usernames: list[str] = []
    for candidate in session.exec(select(User).where(User.is_active == True)).all():  # noqa: E712
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

    def event_stream() -> Iterator[str]:
        yield json.dumps({"type": "start"}, ensure_ascii=False) + "\n"
        try:
            result: dict[str, Any] | None = None
            streamed_reply_parts: list[str] = []
            assistant_context = {
                "project_id": str(project_id) if project_id else None,
                "current_draft": current_draft,
                "target_auth_configured": bool(
                    config.target_headers or config.target_auth_flows
                ),
                "configured_target_header_names": list(config.target_headers),
                "configured_auth_hosts": list(config.target_auth_flows),
                "available_output_formats": ["xlsx", "jsonl", "markdown", "text"],
                "reviewer_type_counts": reviewer_type_counts,
                "available_reviewer_usernames": available_reviewer_usernames,
            }
            if not config.enabled or not config.base_url or not config.model:
                fallback = assist_job_configuration(config, message_history, assistant_context)
                result = {
                    **fallback,
                    "ui_action": fallback.get("ui_action"),
                    "react_trace": [],
                }
                streamed_reply_parts.append(fallback["reply"])
                yield json.dumps(
                    {"type": "delta", "content": fallback["reply"]}, ensure_ascii=False
                ) + "\n"
            else:
                for event_type, value in stream_react_configuration(
                    config, message_history, assistant_context, project_id
                ):
                    if event_type == "delta":
                        streamed_reply_parts.append(str(value))
                        yield json.dumps(
                            {"type": "delta", "content": value}, ensure_ascii=False
                        ) + "\n"
                    elif event_type in {"tool_start", "tool_result"}:
                        yield json.dumps(
                            {"type": event_type, "tool": value}, ensure_ascii=False
                        ) + "\n"
                    else:
                        result = value
            if result is None:
                raise ValueError("Agent model did not return a configuration result")
            draft = {**current_draft, **result["draft"]}
            draft["react_trace"] = result.get("react_trace", [])
            draft["ui_action"] = result.get("ui_action")
            goal = str(draft.get("goal", "")).strip()
            if goal and not str(draft.get("title", "")).strip():
                draft["title"] = derive_task_title(goal)
            if payload.output_format:
                draft["output_format"] = payload.output_format
            has_source = bool(draft.get("source_file_id"))
            has_target = bool(draft.get("target_url"))
            target_ready = has_target and bool(draft.get("target_body"))
            source_ready = has_source and bool(draft.get("source_inspected"))
            target_ready = target_ready and bool(draft.get("target_validated"))
            if draft.get("task_mode") == "human_review":
                core_ready = bool(
                    draft.get("title")
                    and draft.get("goal")
                    and source_ready
                    and (draft.get("reviewer_type_codes") or draft.get("reviewer_usernames"))
                    and draft.get("review_rubric")
                    and draft.get("deadline_hours")
                )
            else:
                core_ready = bool(draft.get("title") and draft.get("goal")) and (
                    source_ready or target_ready
                )
            action_type = (result.get("ui_action") or {}).get("type")
            if action_type == "user_input":
                stage = "collecting"
            elif action_type == "choose_output" or (
                core_ready and not draft.get("output_format")
            ):
                stage = "choose_output"
            elif action_type == "confirm":
                stage = "ready"
            else:
                stage = "collecting"
            # A ReAct exchange can emit useful text before and after several tool
            # calls. Persist the same complete text that the client streamed,
            # instead of replacing it with only the final model turn.
            reply = "".join(streamed_reply_parts) or result["reply"]

            with Session(get_engine()) as write_session:
                stored = write_session.get(AssistantConversation, conversation_id)
                if stored is None or stored.created_by != owner_id:
                    return
                output_attachment = assistant_output_attachment(
                    write_session, result, stored.project_id
                )
                attachment_payload = {
                    "attachment_file_id": (
                        str(output_attachment.id) if output_attachment else None
                    ),
                    "attachment_name": (
                        output_attachment.original_name if output_attachment else None
                    ),
                    "attachment_content_type": (
                        output_attachment.content_type if output_attachment else None
                    ),
                    "attachment_size_bytes": (
                        output_attachment.size_bytes if output_attachment else None
                    ),
                }
                stored.draft = draft
                stored.status = stage
                stored.updated_at = datetime.now(UTC)
                write_session.add(stored)
                write_session.add(
                    AssistantMessage(
                        conversation_id=conversation_id,
                        role="assistant",
                        content=reply,
                        attachment_file_id=(output_attachment.id if output_attachment else None),
                        attachment_name=attachment_payload["attachment_name"],
                        attachment_content_type=attachment_payload[
                            "attachment_content_type"
                        ],
                        attachment_size_bytes=attachment_payload["attachment_size_bytes"],
                    )
                )
                write_session.commit()
            yield (
                json.dumps(
                    {
                        "type": "done",
                        "content": reply,
                        "draft": draft,
                        "stage": stage,
                        **attachment_payload,
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
        except Exception as error:
            yield json.dumps({"type": "error", "message": str(error)}, ensure_ascii=False) + "\n"

    return StreamingResponse(
        event_stream(),
        media_type="application/x-ndjson",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
        },
    )


@router.post(
    "/assistant/conversations/{conversation_id}/started",
    response_model=AssistantConversationRead,
)
def mark_assistant_conversation_started(
    conversation_id: UUID,
    payload: AssistantStartedRequest,
    user: ExperimentRunner,
    session: SessionDependency,
) -> AssistantConversation:
    conversation = require_assistant_conversation(conversation_id, user, session)
    job = require_job(payload.agent_job_id, session)
    if job.created_by != user.id or job.project_id != conversation.project_id:
        raise HTTPException(status_code=422, detail="评测任务与当前对话不匹配")
    already_recorded = (
        conversation.status == "started" and conversation.agent_job_id == job.id
    )
    conversation.agent_job_id = job.id
    conversation.status = "started"
    conversation.updated_at = datetime.now(UTC)
    session.add(conversation)
    if not already_recorded:
        session.add(
            AssistantMessage(
                conversation_id=conversation.id,
                role="user",
                content="确认并开启",
            )
        )
    session.commit()
    session.refresh(conversation)
    return conversation


@router.post(
    "/projects/{project_id}/agent-jobs",
    response_model=AgentJobRead,
    status_code=status.HTTP_201_CREATED,
)
def create_agent_job(
    project_id: UUID,
    payload: AgentJobCreate,
    user: ExperimentRunner,
    session: SessionDependency,
) -> AgentJob:
    require_project(project_id, session)
    if payload.source_file_id:
        file_object = session.get(FileObject, payload.source_file_id)
        if file_object is None or file_object.project_id != project_id:
            raise HTTPException(status_code=422, detail="Source file does not belong to project")
    input_config = dict(payload.input_config)
    input_config["output_format"] = payload.output_format
    if payload.evaluation_model_id:
        input_config["evaluation_model_id"] = str(payload.evaluation_model_id)
    input_config["notification_targets"] = [
        target.model_dump() for target in payload.notification_targets
    ]
    agent_config = get_settings().agent
    job = AgentJob(
        project_id=project_id,
        created_by=user.id,
        source_file_id=payload.source_file_id,
        title=payload.title.strip(),
        goal=payload.goal.strip(),
        input_config=input_config,
        max_repair_attempts=agent_config.max_repair_attempts,
        requires_approval=(
            agent_config.require_approval
            if payload.requires_approval is None
            else payload.requires_approval
        ),
    )
    session.add(job)
    session.commit()
    session.refresh(job)
    return job


@router.get("/projects/{project_id}/agent-jobs", response_model=list[AgentJobRead])
def list_agent_jobs(
    project_id: UUID, _: ExperimentReader, session: SessionDependency
) -> list[AgentJob]:
    require_project(project_id, session)
    statement = select(AgentJob).where(AgentJob.project_id == project_id)
    return list(session.exec(statement.order_by(AgentJob.created_at.desc())).all())


@router.get("/agent-jobs/{job_id}", response_model=AgentJobRead)
def get_agent_job(job_id: UUID, _: ExperimentReader, session: SessionDependency) -> AgentJob:
    return require_job(job_id, session)


@router.get("/agent-jobs/{job_id}/steps", response_model=list[AgentStepRead])
def list_agent_steps(
    job_id: UUID, _: ExperimentReader, session: SessionDependency
) -> list[AgentStep]:
    require_job(job_id, session)
    statement = select(AgentStep).where(AgentStep.job_id == job_id)
    return list(session.exec(statement.order_by(AgentStep.created_at)).all())


@router.get("/agent-jobs/{job_id}/events")
def stream_agent_job_events(
    job_id: UUID,
    _: ExperimentReader,
    session: SessionDependency,
    after: int = 0,
) -> StreamingResponse:
    require_job(job_id, session)

    def event_stream() -> Iterator[str]:
        last_id = max(after, 0)
        last_state = ""
        while True:
            with Session(get_engine()) as read_session:
                statement = (
                    select(AgentJobEvent)
                    .where(AgentJobEvent.job_id == job_id, AgentJobEvent.id > last_id)
                    .order_by(AgentJobEvent.id)
                )
                events = list(read_session.exec(statement).all())
                current_job = read_session.get(AgentJob, job_id)
                steps = list(
                    read_session.exec(
                        select(AgentStep)
                        .where(AgentStep.job_id == job_id)
                        .order_by(AgentStep.created_at)
                    ).all()
                )
            for event in events:
                last_id = int(event.id or last_id)
                data = AgentJobEventRead.model_validate(event).model_dump(mode="json")
                encoded = json.dumps(data, ensure_ascii=False)
                yield f"id: {last_id}\nevent: agent-event\ndata: {encoded}\n\n"
            if current_job is not None:
                state = {
                    "job": AgentJobRead.model_validate(current_job).model_dump(mode="json"),
                    "steps": [
                        AgentStepRead.model_validate(step).model_dump(mode="json")
                        for step in steps
                    ],
                }
                encoded_state = json.dumps(state, ensure_ascii=False)
                if encoded_state != last_state:
                    last_state = encoded_state
                    yield f"event: job-state\ndata: {encoded_state}\n\n"
            terminal = current_job is None or current_job.status in {
                AgentJobStatus.COMPLETED,
                AgentJobStatus.FAILED,
                AgentJobStatus.CANCELLED,
            }
            if terminal and not events:
                yield "event: end\ndata: {}\n\n"
                return
            if not events:
                yield ": keep-alive\n\n"
            time.sleep(0.35)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/agent-jobs/{job_id}/start", response_model=AgentJobRead)
def start_agent_job(job_id: UUID, _: ExperimentRunner, session: SessionDependency) -> AgentJob:
    job = require_job(job_id, session)
    if job.status not in {
        AgentJobStatus.PENDING,
        AgentJobStatus.FAILED,
        AgentJobStatus.COMPLETED,
        AgentJobStatus.CANCELLED,
    }:
        raise HTTPException(status_code=409, detail="Agent job cannot be started in current state")
    job.status = AgentJobStatus.PENDING
    job.error = None
    job.eval_spec = {}
    job.result = {}
    job.result_file_id = None
    session.add(job)
    session.commit()
    enqueue_agent_task("evalweave.agent.plan", job.id)
    session.refresh(job)
    return job


@router.get("/human-tasks", response_model=list[HumanTaskRead])
def list_human_tasks(
    _: EvaluationReviewer,
    session: SessionDependency,
    task_status: HumanTaskStatus | None = None,
) -> list[HumanTask]:
    statement = select(HumanTask)
    if task_status is not None:
        statement = statement.where(HumanTask.status == task_status)
    return list(session.exec(statement.order_by(HumanTask.created_at.desc())).all())


@router.get("/human-tasks/{task_id}", response_model=HumanTaskRead)
def get_human_task(task_id: UUID, _: EvaluationReviewer, session: SessionDependency) -> HumanTask:
    return require_human_task(task_id, session)


@router.post("/human-tasks/{task_id}/decision", response_model=HumanTaskRead)
def decide_human_task(
    task_id: UUID,
    payload: HumanDecision,
    user: EvaluationReviewer,
    session: SessionDependency,
) -> HumanTask:
    task = require_human_task(task_id, session)
    if task.status != HumanTaskStatus.PENDING:
        raise HTTPException(status_code=409, detail="Human task has already been resolved")
    job = require_job(task.job_id, session)
    task.status = (
        HumanTaskStatus.APPROVED if payload.decision == "approve" else HumanTaskStatus.REJECTED
    )
    task.decision_reason = payload.reason
    task.resolved_by = user.id
    task.resolved_at = datetime.now(UTC)
    task.updated_at = datetime.now(UTC)
    if task.status == HumanTaskStatus.APPROVED:
        job.status = AgentJobStatus.RUNNING
    else:
        job.status = AgentJobStatus.CANCELLED
    job.updated_at = datetime.now(UTC)
    session.add(task)
    session.add(job)
    session.commit()
    if task.status == HumanTaskStatus.APPROVED:
        enqueue_agent_task("evalweave.agent.execute", job.id)
    session.refresh(task)
    return task


@router.post("/human-tasks/{task_id}/notify", response_model=list[dict[str, Any]])
def resend_human_task_notification(
    task_id: UUID, _: EvaluationReviewer, session: SessionDependency
) -> list[dict[str, Any]]:
    task = require_human_task(task_id, session)
    deliveries = notify_human_task(session, task)
    return [
        {
            "id": str(item.id),
            "channel": item.channel,
            "recipient": item.recipient,
            "status": item.status,
            "error": item.error,
        }
        for item in deliveries
    ]


@router.get("/human-tasks/{task_id}/deliveries", response_model=list[dict[str, Any]])
def list_notification_deliveries(
    task_id: UUID, _: EvaluationReviewer, session: SessionDependency
) -> list[dict[str, Any]]:
    require_human_task(task_id, session)
    statement = select(NotificationDelivery).where(NotificationDelivery.human_task_id == task_id)
    deliveries = session.exec(statement.order_by(NotificationDelivery.created_at.desc())).all()
    return [
        {
            "id": str(item.id),
            "channel": item.channel,
            "recipient": item.recipient,
            "status": item.status,
            "error": item.error,
            "sent_at": item.sent_at,
        }
        for item in deliveries
    ]
