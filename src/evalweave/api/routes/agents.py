from __future__ import annotations

import json
import re
import time
from collections.abc import Iterator
from queue import Queue
from threading import Lock, Thread
from typing import Annotated, Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Response, status
from fastapi.responses import StreamingResponse

from evalweave.agents.model_config import resolve_agent_config
from evalweave.agents.planner import (
    assist_job_configuration,
    derive_task_title,
)
from evalweave.agents.runtime import stream_react_configuration
from evalweave.api.response import APIResponse
from evalweave.api.schemas.agents import (
    AgentAssistRead,
    AgentAssistRequest,
    AgentJobCreate,
    AgentJobEventRead,
    AgentJobRead,
    AgentRuntimeRead,
    AgentStepRead,
    AssistantConversationCreate,
    AssistantConversationRead,
    AssistantConversationUpdate,
    AssistantMessageRead,
    AssistantStartedRequest,
    AssistantStreamRequest,
    EvaluationModelOption,
    HumanDecision,
    HumanTaskRead,
)
from evalweave.auth.dependencies import SessionDependency, require_permission
from evalweave.auth.permissions import Permission
from evalweave.core.config import get_settings
from evalweave.db.models import (
    AgentJob,
    AgentJobStatus,
    AgentStep,
    AssistantConversation,
    AssistantMessage,
    EvaluationModel,
    HumanTask,
    HumanTaskStatus,
    User,
)
from evalweave.db.session import get_engine
from evalweave.services.agent_jobs import AgentJobService
from evalweave.services.assistant import (
    DEFAULT_CONVERSATION_TITLE,
    AssistantService,
    AssistantStreamPersister,
)
from evalweave.services.assistant import (
    normalize_conversation_title as _normalize_conversation_title,
)
from evalweave.services.human_tasks import HumanTaskService
from evalweave.services.streaming import StreamingService
from evalweave.workers.factory import create_celery_app

router = APIRouter(tags=["evaluation-agent"])

_WORKER_PROBE_TTL_SECONDS = 5.0
_worker_probe_lock = Lock()
_worker_probe_available = False
_worker_probe_checked_at = 0.0
_worker_probe_running = False

ExperimentReader = Annotated[User, Depends(require_permission(Permission.EXPERIMENT_READ))]
ExperimentRunner = Annotated[User, Depends(require_permission(Permission.EXPERIMENT_RUN))]
EvaluationReviewer = Annotated[User, Depends(require_permission(Permission.EVALUATION_REVIEW))]


def redact_sensitive_content(content: str) -> str:
    """Keep user-provided task content unchanged when it is persisted."""
    return content


def sanitize_assistant_display(content: str) -> str:
    internal_file_target = r"(?:https?://[^)\s]+)?[^)\r\n]*/api/(?:projects?|files?)/[^)\r\n]+"
    content = re.sub(
        rf"(?im)^[ \t]*(?:📄[ \t]*)?(?:\*\*)?文件下载[：:]"
        rf"(?:\*\*)?[ \t]*\[[^\]\r\n]+\]\({internal_file_target}\)[ \t]*\r?\n?",
        "",
        content,
    )
    content = re.sub(
        rf"\[([^\]\r\n]+)\]\({internal_file_target}\)",
        r"\1",
        content,
    )
    content = re.sub(
        r"(?i)文件在\s*[`*]*(?:inputs?)[\\/][`*]*\s*(?:里|中)?[，,]?\s*"
        r"直接复制过去并重命名[：:]?",
        "正在重命名文件：",
        content,
    )
    content = re.sub(
        r"(?i)[，,]?\s*(?:生成|保存|写入)(?:在|到)\s*[`*]*(?:outputs?)[\\/]"
        r"[`*]*\s*(?:目录)?(?:下|中)?",
        "，文件已生成",
        content,
    )
    return re.sub(r"(?i)[`*]*(?:inputs?|outputs?)[\\/][`*]*", "", content)


def infer_explicit_output_format(content: str) -> str | None:
    """Recognize a result format only when the user states one explicitly."""
    lowered = content.casefold()
    patterns = (
        ("xlsx", r"(?:\.xlsx?\b|\bexcel\b|电子表格)"),
        ("jsonl", r"(?:\.jsonl?\b|\bjsonl\b|\bjson\s*(?:文件|格式))"),
        ("markdown", r"(?:\.md\b|\.markdown\b|\bmarkdown\b|markdown\s*文件)"),
        ("text", r"(?:\.txt\b|纯文本|txt\s*文件|不需要文件|直接在对话中(?:查看|展示))"),
    )
    for output_format, pattern in patterns:
        if re.search(pattern, lowered, flags=re.IGNORECASE):
            return output_format
    return None


def enqueue_agent_task(task_name: str, job_id: UUID) -> None:
    create_celery_app().send_task(task_name, args=[str(job_id)])


def _refresh_worker_status() -> None:
    global _worker_probe_available, _worker_probe_checked_at, _worker_probe_running
    try:
        available = bool(create_celery_app().control.inspect(timeout=1.5).ping())
    except Exception:
        available = False
    with _worker_probe_lock:
        _worker_probe_available = available
        _worker_probe_checked_at = time.monotonic()
        _worker_probe_running = False


def cached_worker_status() -> bool:
    """Return immediately and refresh a stale Celery status in the background."""
    global _worker_probe_running
    with _worker_probe_lock:
        stale = time.monotonic() - _worker_probe_checked_at >= _WORKER_PROBE_TTL_SECONDS
        if stale and not _worker_probe_running:
            _worker_probe_running = True
            Thread(
                target=_refresh_worker_status,
                name="worker-status-probe",
                daemon=True,
            ).start()
        return _worker_probe_available


def require_job(job_id: UUID, session: SessionDependency) -> AgentJob:
    return AgentJobService.require(session, job_id)


def require_user_job(job_id: UUID, user: User, session: SessionDependency) -> AgentJob:
    return AgentJobService.require_for_user(session, job_id, user)


def require_human_task(task_id: UUID, session: SessionDependency) -> HumanTask:
    return HumanTaskService.require(session, task_id)


def require_assistant_conversation(
    conversation_id: UUID, user: User, session: SessionDependency
) -> AssistantConversation:
    return AssistantService.require_conversation(session, conversation_id, user)


@router.get("/agent/runtime", response_model=APIResponse[AgentRuntimeRead])
def get_agent_runtime(_: ExperimentReader) -> APIResponse[AgentRuntimeRead]:
    config = get_settings().agent
    return APIResponse.success(
        AgentRuntimeRead(
            enabled=config.enabled,
            model=config.model or None,
            require_approval=config.require_approval,
            worker_available=cached_worker_status(),
        )
    )


@router.get(
    "/evaluation-models",
    response_model=APIResponse[list[EvaluationModelOption]],
)
def list_available_evaluation_models(
    _: ExperimentReader, session: SessionDependency
) -> APIResponse[list[EvaluationModel]]:
    return APIResponse.success(AgentJobService.list_evaluation_models(session))


@router.post("/agent/assist", response_model=APIResponse[AgentAssistRead])
def assist_with_agent(
    payload: AgentAssistRequest, _: ExperimentRunner, session: SessionDependency
) -> APIResponse[AgentAssistRead]:
    config = resolve_agent_config(session, payload.evaluation_model_id)
    result = assist_job_configuration(
        config,
        [message.model_dump() for message in payload.messages],
        {
            "project_id": str(payload.project_id) if payload.project_id else None,
            "available_output_formats": ["xlsx", "jsonl", "markdown", "text"],
        },
    )
    return APIResponse.success(AgentAssistRead.model_validate(result))


@router.get(
    "/assistant/conversations",
    response_model=APIResponse[list[AssistantConversationRead]],
)
def list_assistant_conversations(
    user: ExperimentRunner,
    session: SessionDependency,
    project_id: UUID | None = None,
) -> APIResponse[list[AssistantConversation]]:
    return APIResponse.success(AssistantService.list_conversations(session, user, project_id))


@router.post(
    "/assistant/conversations",
    response_model=APIResponse[AssistantConversationRead],
    status_code=status.HTTP_201_CREATED,
)
def create_assistant_conversation(
    payload: AssistantConversationCreate,
    user: ExperimentRunner,
    session: SessionDependency,
) -> APIResponse[AssistantConversation]:
    return APIResponse.success(
        AssistantService.create_conversation(session, user, payload.project_id)
    )


def normalize_conversation_title(value: str, question: str) -> str:
    """Compatibility wrapper for callers of the former route helper."""
    return _normalize_conversation_title(value, question)


def generate_conversation_title(
    conversation_id: UUID,
    owner_id: UUID,
    question: str,
    evaluation_model_id: UUID | None,
) -> None:
    AssistantService.generate_conversation_title(
        get_engine(),
        conversation_id,
        owner_id,
        question,
        evaluation_model_id,
    )


def schedule_conversation_title(
    conversation_id: UUID,
    owner_id: UUID,
    question: str,
    evaluation_model_id: UUID | None,
) -> None:
    Thread(
        target=generate_conversation_title,
        args=(conversation_id, owner_id, question, evaluation_model_id),
        daemon=True,
        name=f"conversation-title-{conversation_id}",
    ).start()


@router.patch(
    "/assistant/conversations/{conversation_id}",
    response_model=APIResponse[AssistantConversationRead],
)
def update_assistant_conversation(
    conversation_id: UUID,
    payload: AssistantConversationUpdate,
    user: ExperimentRunner,
    session: SessionDependency,
) -> APIResponse[AssistantConversation]:
    return APIResponse.success(
        AssistantService.update_conversation(
            session,
            conversation_id,
            user,
            payload.title,
        )
    )


@router.delete(
    "/assistant/conversations/{conversation_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_assistant_conversation(
    conversation_id: UUID,
    user: ExperimentRunner,
    session: SessionDependency,
) -> Response:
    AssistantService.delete_conversation(session, conversation_id, user)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/assistant/conversations/{conversation_id}/messages",
    response_model=APIResponse[list[AssistantMessageRead]],
)
def list_assistant_messages(
    conversation_id: UUID,
    user: ExperimentRunner,
    session: SessionDependency,
) -> APIResponse[list[AssistantMessage]]:
    return APIResponse.success(AssistantService.list_messages(session, conversation_id, user))


@router.get("/assistant/conversations/{conversation_id}/messages/events")
def stream_assistant_messages(
    conversation_id: UUID,
    user: ExperimentRunner,
    session: SessionDependency,
) -> StreamingResponse:
    require_assistant_conversation(conversation_id, user, session)
    owner_id = user.id

    def produce_events() -> Iterator[str]:
        previous_signature = ""
        while True:
            items = StreamingService.assistant_messages(
                get_engine(),
                conversation_id,
                owner_id,
            )
            if items is None:
                return
            payload = [
                AssistantMessageRead.model_validate(item).model_dump(mode="json") for item in items
            ]
            signature = json.dumps(
                [
                    (
                        item["id"],
                        item["content"],
                        item["is_streaming"],
                        item["ui_action"],
                    )
                    for item in payload
                ],
                ensure_ascii=False,
            )
            if signature != previous_signature:
                yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
                previous_signature = signature
            if not any(bool(item["is_streaming"]) for item in payload):
                return
            time.sleep(0.25)

    return StreamingResponse(
        produce_events(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/assistant/conversations/{conversation_id}/title/events")
def stream_assistant_conversation_title(
    conversation_id: UUID,
    user: ExperimentRunner,
    session: SessionDependency,
) -> StreamingResponse:
    require_assistant_conversation(conversation_id, user, session)
    owner_id = user.id

    def produce_events() -> Iterator[str]:
        while True:
            conversation = StreamingService.conversation_title(
                get_engine(),
                conversation_id,
                owner_id,
            )
            if conversation is None:
                return
            if conversation.title != DEFAULT_CONVERSATION_TITLE:
                payload = AssistantConversationRead.model_validate(conversation).model_dump(
                    mode="json"
                )
                yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
                return
            yield ": keep-alive\n\n"
            time.sleep(0.25)

    return StreamingResponse(
        produce_events(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/assistant/conversations/{conversation_id}/messages/stream")
def stream_assistant_message(
    conversation_id: UUID,
    payload: AssistantStreamRequest,
    user: ExperimentRunner,
    session: SessionDependency,
) -> StreamingResponse:
    conversation = require_assistant_conversation(conversation_id, user, session)
    explicit_output_format = payload.output_format or infer_explicit_output_format(payload.content)
    config = resolve_agent_config(session, payload.evaluation_model_id)
    turn = AssistantService.prepare_turn(
        session,
        conversation,
        user,
        content=redact_sensitive_content(payload.content.strip()),
        source_file_id=payload.source_file_id,
        evaluation_model_id=payload.evaluation_model_id,
        explicit_output_format=explicit_output_format,
        config=config,
        initial_message_id=uuid4(),
    )
    if turn.should_generate_title:
        schedule_conversation_title(
            conversation.id,
            user.id,
            payload.content.strip(),
            payload.evaluation_model_id,
        )
    message_history = turn.message_history
    current_draft = turn.current_draft
    project_id = turn.project_id
    owner_id = turn.owner_id
    assistant_context = turn.assistant_context
    stream_persister = AssistantStreamPersister(get_engine())

    def produce_events() -> Iterator[str]:
        active_message_id = turn.initial_message_id
        stream_completed = False
        yield json.dumps({"type": "start"}, ensure_ascii=False) + "\n"
        try:
            result: dict[str, Any] | None = None
            current_reply_parts: list[str] = []
            last_persisted_at = 0.0
            if not config.enabled or not config.base_url or not config.model:
                fallback = assist_job_configuration(config, message_history, assistant_context)
                result = {
                    **fallback,
                    "ui_action": fallback.get("ui_action"),
                    "react_trace": [],
                }
                current_reply_parts.append(fallback["reply"])
                yield (
                    json.dumps({"type": "delta", "content": fallback["reply"]}, ensure_ascii=False)
                    + "\n"
                )
            else:
                for event_type, value in stream_react_configuration(
                    config,
                    message_history,
                    assistant_context,
                    project_id,
                    owner_id,
                    conversation.id,
                ):
                    if event_type == "delta":
                        current_reply_parts.append(str(value))
                        yield (
                            json.dumps({"type": "delta", "content": value}, ensure_ascii=False)
                            + "\n"
                        )
                        now = time.monotonic()
                        if now - last_persisted_at >= 0.25:
                            stream_persister.submit(
                                active_message_id,
                                sanitize_assistant_display("".join(current_reply_parts)),
                            )
                            last_persisted_at = now
                    elif event_type == "round_end":
                        round_reply = sanitize_assistant_display(
                            "".join(current_reply_parts)
                        ).strip()
                        stream_persister.flush()
                        next_message_id = uuid4()
                        AssistantService.advance_streaming_message(
                            get_engine(),
                            conversation_id,
                            active_message_id,
                            round_reply,
                            next_message_id,
                        )
                        active_message_id = next_message_id
                        current_reply_parts = []
                        last_persisted_at = 0.0
                        yield json.dumps({"type": "round_end"}, ensure_ascii=False) + "\n"
                    elif event_type in {"tool_start", "tool_result"}:
                        yield (
                            json.dumps({"type": event_type, "tool": value}, ensure_ascii=False)
                            + "\n"
                        )
                    else:
                        result = value
            if result is None:
                raise ValueError("Agent model did not return a configuration result")
            draft = {**current_draft, **result["draft"]}
            draft["react_trace"] = result.get("react_trace", [])
            # UI actions belong to the assistant message that produced them. Keeping
            # them in the conversation draft makes stale controls move to later replies.
            draft.pop("ui_action", None)
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
            elif action_type == "choose_output" or (core_ready and not draft.get("output_format")):
                stage = "choose_output"
            elif action_type in {"confirm", "start_task"}:
                stage = "ready"
            else:
                stage = "collecting"
            # Intermediate ReAct narration remains visible as separate bubbles, but
            # only the final answer participates in future model context.
            reply = sanitize_assistant_display(str(result["reply"]))

            stream_persister.flush()
            attachment_payload = AssistantService.complete_turn(
                get_engine(),
                conversation_id,
                owner_id,
                draft,
                stage,
                result,
            )
            if attachment_payload is None:
                return
            AssistantService.persist_streaming_message(
                get_engine(),
                active_message_id,
                reply,
                is_streaming=False,
                include_in_context=True,
                ui_action=result.get("ui_action"),
                attachment=attachment_payload,
            )
            stream_completed = True
            yield (
                json.dumps(
                    {
                        "type": "done",
                        "content": reply,
                        "draft": draft,
                        "stage": stage,
                        "ui_action": result.get("ui_action"),
                        **attachment_payload,
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
        except Exception as error:
            stream_persister.flush()
            AssistantService.persist_streaming_message(
                get_engine(),
                active_message_id,
                sanitize_assistant_display("".join(current_reply_parts)),
                is_streaming=False,
                include_in_context=False,
            )
            yield json.dumps({"type": "error", "message": str(error)}, ensure_ascii=False) + "\n"
        finally:
            stream_persister.flush()
            if not stream_completed:
                AssistantService.persist_streaming_message(
                    get_engine(),
                    active_message_id,
                    sanitize_assistant_display("".join(current_reply_parts)),
                    is_streaming=False,
                    include_in_context=False,
                )

    event_queue: Queue[str | None] = Queue()

    def run_in_background() -> None:
        try:
            for event in produce_events():
                event_queue.put(event)
        finally:
            stream_persister.close()
            event_queue.put(None)

    Thread(
        target=run_in_background,
        name=f"assistant-{conversation_id}",
        daemon=True,
    ).start()

    def event_stream() -> Iterator[str]:
        while True:
            event = event_queue.get()
            if event is None:
                return
            yield event

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
    response_model=APIResponse[AssistantConversationRead],
)
def mark_assistant_conversation_started(
    conversation_id: UUID,
    payload: AssistantStartedRequest,
    user: ExperimentRunner,
    session: SessionDependency,
) -> APIResponse[AssistantConversation]:
    return APIResponse.success(
        AssistantService.mark_started(
            session,
            conversation_id,
            payload.agent_job_id,
            user,
        )
    )


@router.post(
    "/projects/{project_id}/agent-jobs",
    response_model=APIResponse[AgentJobRead],
    status_code=status.HTTP_201_CREATED,
)
def create_agent_job(
    project_id: UUID,
    payload: AgentJobCreate,
    user: ExperimentRunner,
    session: SessionDependency,
) -> APIResponse[AgentJob]:
    values = payload.model_dump()
    values["notification_targets"] = [
        target.model_dump() for target in payload.notification_targets
    ]
    return APIResponse.success(AgentJobService.create(session, project_id, user, values))


@router.get(
    "/projects/{project_id}/agent-jobs",
    response_model=APIResponse[list[AgentJobRead]],
)
def list_agent_jobs(
    project_id: UUID, user: ExperimentReader, session: SessionDependency
) -> APIResponse[list[AgentJob]]:
    return APIResponse.success(AgentJobService.list(session, project_id, user))


@router.get("/agent-jobs/{job_id}", response_model=APIResponse[AgentJobRead])
def get_agent_job(
    job_id: UUID, user: ExperimentReader, session: SessionDependency
) -> APIResponse[AgentJob]:
    return APIResponse.success(require_user_job(job_id, user, session))


@router.get(
    "/agent-jobs/{job_id}/steps",
    response_model=APIResponse[list[AgentStepRead]],
)
def list_agent_steps(
    job_id: UUID, user: ExperimentReader, session: SessionDependency
) -> APIResponse[list[AgentStep]]:
    return APIResponse.success(AgentJobService.list_steps(session, job_id, user))


@router.get("/agent-jobs/{job_id}/events")
def stream_agent_job_events(
    job_id: UUID,
    user: ExperimentReader,
    session: SessionDependency,
    after: int = 0,
) -> StreamingResponse:
    require_user_job(job_id, user, session)

    def event_stream() -> Iterator[str]:
        last_id = max(after, 0)
        last_state = ""
        while True:
            events, current_job, steps = StreamingService.job_snapshot(
                get_engine(),
                job_id,
                last_id,
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
                        AgentStepRead.model_validate(step).model_dump(mode="json") for step in steps
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


@router.post("/agent-jobs/{job_id}/start", response_model=APIResponse[AgentJobRead])
def start_agent_job(
    job_id: UUID, user: ExperimentRunner, session: SessionDependency
) -> APIResponse[AgentJob]:
    job, task_name = AgentJobService.restart(session, job_id, user)
    enqueue_agent_task(task_name, job.id)
    return APIResponse.success(job)


@router.get("/human-tasks", response_model=APIResponse[list[HumanTaskRead]])
def list_human_tasks(
    _: EvaluationReviewer,
    session: SessionDependency,
    task_status: HumanTaskStatus | None = None,
) -> APIResponse[list[HumanTask]]:
    return APIResponse.success(HumanTaskService.list(session, task_status))


@router.get("/human-tasks/{task_id}", response_model=APIResponse[HumanTaskRead])
def get_human_task(
    task_id: UUID, _: EvaluationReviewer, session: SessionDependency
) -> APIResponse[HumanTask]:
    return APIResponse.success(require_human_task(task_id, session))


@router.post(
    "/human-tasks/{task_id}/decision",
    response_model=APIResponse[HumanTaskRead],
)
def decide_human_task(
    task_id: UUID,
    payload: HumanDecision,
    user: EvaluationReviewer,
    session: SessionDependency,
) -> APIResponse[HumanTask]:
    task, approved = HumanTaskService.decide(
        session,
        task_id,
        user,
        payload.decision,
        payload.reason,
    )
    if approved:
        enqueue_agent_task("evalweave.agent.execute", task.job_id)
    return APIResponse.success(task)


@router.post(
    "/human-tasks/{task_id}/notify",
    response_model=APIResponse[list[dict[str, Any]]],
)
def resend_human_task_notification(
    task_id: UUID, _: EvaluationReviewer, session: SessionDependency
) -> APIResponse[list[dict[str, Any]]]:
    return APIResponse.success(HumanTaskService.resend_notification(session, task_id))


@router.get(
    "/human-tasks/{task_id}/deliveries",
    response_model=APIResponse[list[dict[str, Any]]],
)
def list_notification_deliveries(
    task_id: UUID, _: EvaluationReviewer, session: SessionDependency
) -> APIResponse[list[dict[str, Any]]]:
    return APIResponse.success(HumanTaskService.list_deliveries(session, task_id))
