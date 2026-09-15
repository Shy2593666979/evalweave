from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlmodel import Session, select

from evalweave.agents.events import ModelEventPublisher
from evalweave.agents.executor import (
    execute_data_program,
    execute_http_target,
    execute_local_source,
    normalize_target_body,
    validate_http_target,
)
from evalweave.agents.inspection import inspect_source, load_source_rows
from evalweave.agents.model_config import resolve_agent_config
from evalweave.agents.planner import (
    evaluate_target_records,
    generate_eval_spec,
    generate_summary,
    generate_test_cases,
    model_map_rows,
)
from evalweave.agents.python_workspace import run_python_workspace
from evalweave.agents.react_runtime import stream_react_configuration
from evalweave.core.config import get_settings
from evalweave.db.models import (
    AgentJob,
    AgentJobStatus,
    AgentStep,
    AssistantConversation,
    AssistantMessage,
    FileObject,
    StepStatus,
)
from evalweave.db.session import get_engine
from evalweave.notifications import notify_agent_job_completed
from evalweave.storage import LocalFileStorage

StepCallable = Callable[[], dict[str, Any]]
logger = logging.getLogger(__name__)


def run_streamed_model_output(
    job: AgentJob,
    phase: str,
    label: str,
    operation: Callable[[Callable[[str], None]], Any],
) -> Any:
    publisher = ModelEventPublisher(job.id, phase, label)
    try:
        result = operation(publisher.write)
    except Exception as error:
        publisher.fail(error)
        raise
    publisher.complete()
    return result


def return_error_to_assistant(session: Session, job: AgentJob, error: Exception) -> None:
    conversation = session.exec(
        select(AssistantConversation).where(AssistantConversation.agent_job_id == job.id)
    ).first()
    if conversation is None:
        return
    message = str(error)
    if "401" in message or "Unauthorized" in message:
        reply = (
            "目标接口返回了 401 未授权，目前缺少有效鉴权配置。请确认接口使用的鉴权方式，"
            "并由管理员配置对应请求头后再继续；系统不会继续发送剩余测试请求。"
        )
    elif "422" in message:
        reply = (
            "目标接口返回了 422，请补充一份可成功调用的请求 JSON 示例。"
            "我会据此调整测试 case 的 input 结构后重新预检。"
        )
    else:
        reply = f"目标接口预检没有通过：{message}。请补充接口调用要求后，我会重新验证。"
    conversation.status = "collecting"
    conversation.updated_at = datetime.now(UTC)
    session.add(conversation)
    session.add(
        AssistantMessage(
            conversation_id=conversation.id,
            role="assistant",
            content=reply,
        )
    )
    session.commit()


def update_job(session: Session, job: AgentJob, status: AgentJobStatus) -> None:
    job.status = status
    job.updated_at = datetime.now(UTC)
    session.add(job)
    session.commit()


def run_step(
    session: Session,
    job: AgentJob,
    name: str,
    operation: StepCallable,
    input_data: dict[str, Any] | None = None,
    attempt: int = 1,
) -> dict[str, Any]:
    step = session.exec(
        select(AgentStep)
        .where(
            AgentStep.job_id == job.id,
            AgentStep.name == name,
            AgentStep.status == StepStatus.PENDING,
        )
        .order_by(AgentStep.created_at)
    ).first()
    if step is None:
        step = AgentStep(job_id=job.id, name=name)
    step.status = StepStatus.RUNNING
    step.attempt = attempt
    step.input_data = input_data or {}
    step.started_at = datetime.now(UTC)
    step.finished_at = None
    step.error = None
    session.add(step)
    session.commit()
    try:
        output = operation()
    except Exception as error:
        step.status = StepStatus.FAILED
        step.error = str(error)[:4000]
        step.finished_at = datetime.now(UTC)
        step.updated_at = datetime.now(UTC)
        session.add(step)
        session.commit()
        raise
    step.status = StepStatus.COMPLETED
    step.output_data = output
    step.finished_at = datetime.now(UTC)
    step.updated_at = datetime.now(UTC)
    session.add(step)
    session.commit()
    return output


def ensure_pending_steps(session: Session, job: AgentJob, names: list[str]) -> None:
    """Create not-yet-executed steps once their task plan is known."""
    for name in names:
        existing = session.exec(
            select(AgentStep).where(
                AgentStep.job_id == job.id,
                AgentStep.name == name,
                AgentStep.status.in_([StepStatus.PENDING, StepStatus.RUNNING]),
            )
        ).first()
        if existing is None:
            session.add(AgentStep(job_id=job.id, name=name, status=StepStatus.PENDING))
    session.commit()


def run_step_with_retries(
    session: Session,
    job: AgentJob,
    name: str,
    operation: StepCallable,
    input_data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    last_error: Exception | None = None
    for attempt in range(1, job.max_repair_attempts + 2):
        try:
            return run_step(
                session,
                job,
                name,
                operation,
                input_data,
                attempt=attempt,
            )
        except Exception as error:
            last_error = error
            if attempt > job.max_repair_attempts:
                raise
            time.sleep(min(2 ** (attempt - 1), 4))
    raise last_error or RuntimeError(f"Agent step failed: {name}")


def execute_python_job(job_id: UUID) -> None:
    """Execute a tool-planned evaluation whose implementation uses Python."""
    with Session(get_engine()) as session:
        job = session.get(AgentJob, job_id)
        if job is None or job.status in {
            AgentJobStatus.COMPLETED,
            AgentJobStatus.CANCELLED,
        }:
            return
        if job.input_config.get("job_type") != "python":
            raise ValueError("任务不是 Python 后台任务")
        ensure_pending_steps(session, job, ["execute_eval_spec", "summarize"])
        update_job(session, job, AgentJobStatus.RUNNING)
        try:
            source_file_ids = job.input_config.get("source_file_ids")
            if not isinstance(source_file_ids, list):
                source_file_ids = None
            observation = run_step(
                session,
                job,
                "execute_eval_spec",
                lambda: run_python_workspace(
                    session,
                    job.project_id,
                    {},
                    str(job.input_config.get("python_code") or ""),
                    [str(item) for item in source_file_ids]
                    if source_file_ids is not None
                    else None,
                    str(job.input_config.get("primary_output") or "") or None,
                    created_by=job.created_by,
                    timeout_seconds=None,
                )[0],
            )
            raw_file_id = observation.get("primary_output_file_id")
            job.result_file_id = UUID(str(raw_file_id)) if raw_file_id else None
            update_job(session, job, AgentJobStatus.ANALYZING)

            def summarize_python_evaluation() -> dict[str, Any]:
                outputs = observation.get("outputs", [])
                execution = {
                    "execution_mode": "agent_python",
                    "evaluation_plan": job.eval_spec,
                    "outputs": outputs,
                    "stdout": observation.get("stdout", ""),
                }
                if job.result_file_id:
                    result_file = session.get(FileObject, job.result_file_id)
                    if result_file is not None:
                        path = LocalFileStorage(
                            get_settings().storage.local_directory
                        ).path_for(result_file.storage_key)
                        extension = Path(result_file.original_name).suffix.lower()
                        preview: dict[str, Any] = {
                            "file_name": result_file.original_name,
                            "content_type": result_file.content_type,
                        }
                        try:
                            if extension in {".csv", ".json", ".jsonl", ".xlsx"}:
                                preview["rows"] = load_source_rows(
                                    path,
                                    result_file.original_name,
                                    100,
                                )
                            elif extension in {".md", ".markdown", ".txt"}:
                                preview["content"] = path.read_text(
                                    encoding="utf-8-sig"
                                )[:20000]
                        except Exception as error:
                            preview["preview_error"] = str(error)[:500]
                        execution["primary_result"] = preview
                agent_config = resolve_agent_config(
                    session, job.input_config.get("evaluation_model_id")
                )
                summary = run_streamed_model_output(
                    job,
                    "summarize",
                    "总结评测结果",
                    lambda on_delta: generate_summary(
                        agent_config,
                        job.goal,
                        execution,
                        on_delta=on_delta,
                    ),
                )
                summary["execution"] = {
                    "execution_mode": "agent_python",
                    "outputs": outputs,
                    "stdout": observation.get("stdout", ""),
                }
                return summary

            job.result = run_step(
                session,
                job,
                "summarize",
                summarize_python_evaluation,
            )
            job.error = None
            update_job(session, job, AgentJobStatus.COMPLETED)
            try:
                notify_agent_job_completed(session, job)
            except Exception:
                logger.exception("Failed to notify completed Python job %s", job.id)
        except Exception as error:
            session.rollback()
            job = session.get(AgentJob, job_id)
            if job is not None:
                job.status = AgentJobStatus.FAILED
                job.error = str(error)[:4000]
                job.updated_at = datetime.now(UTC)
                session.add(job)
                session.commit()


def _start_react_step(
    session: Session, job: AgentJob, name: str, attempt: int
) -> AgentStep:
    step = AgentStep(
        job_id=job.id,
        name=name[:64],
        status=StepStatus.RUNNING,
        attempt=attempt,
        started_at=datetime.now(UTC),
    )
    session.add(step)
    session.commit()
    session.refresh(step)
    return step


def return_result_to_assistant(
    session: Session,
    job: AgentJob,
    result: dict[str, Any],
) -> None:
    conversation = session.exec(
        select(AssistantConversation).where(AssistantConversation.agent_job_id == job.id)
    ).first()
    summary = result.get("summary")
    if conversation is None or not isinstance(summary, str) or not summary.strip():
        return
    reply = summary.strip()
    existing = session.exec(
        select(AssistantMessage).where(
            AssistantMessage.conversation_id == conversation.id,
            AssistantMessage.role == "assistant",
            AssistantMessage.content == reply,
        )
    ).first()
    if existing is not None:
        return
    conversation.updated_at = datetime.now(UTC)
    session.add(conversation)
    session.add(
        AssistantMessage(
            conversation_id=conversation.id,
            role="assistant",
            content=reply,
        )
    )
    session.commit()


def _finish_react_step(
    session: Session, step: AgentStep, tool_result: dict[str, Any]
) -> None:
    succeeded = bool(tool_result.get("status") == "completed")
    summary = tool_result.get("summary")
    step.status = StepStatus.COMPLETED if succeeded else StepStatus.FAILED
    step.output_data = summary if isinstance(summary, dict) else {"result": summary}
    if not succeeded:
        error = summary.get("error") if isinstance(summary, dict) else summary
        step.error = str(error or "Agent tool failed")[:4000]
    step.finished_at = datetime.now(UTC)
    step.updated_at = datetime.now(UTC)
    session.add(step)
    session.commit()


def repair_http_target_with_react(
    session: Session,
    job: AgentJob,
    agent_config: Any,
    error: Exception,
    attempt: int,
) -> bool:
    """Give a failed HTTP probe back to the native tool-calling agent."""
    if not agent_config.enabled or not agent_config.base_url or not agent_config.model:
        return False
    target = job.input_config.get("target")
    if not isinstance(target, dict):
        return False
    cases = job.eval_spec.get("generated_cases")
    sample_cases = cases[:3] if isinstance(cases, list) else []
    draft = {
        "title": job.title,
        "goal": job.goal,
        "task_mode": "dataset_target" if job.source_file_id else "generated_target",
        "source_file_id": str(job.source_file_id) if job.source_file_id else "",
        "target_url": target.get("url"),
        "target_body": normalize_target_body(target.get("body", "{{row}}")),
        "response_path": target.get("response_path", ""),
        "expected_streaming": target.get("expected_streaming"),
        "target_validated": False,
        "output_format": job.input_config.get("output_format", "text"),
        "max_cases": job.input_config.get("max_cases", 100),
    }
    observation = {
        "failed_action": "probe_http_target",
        "error": str(error)[:4000],
        "target": draft,
        "sample_cases": sample_cases,
    }
    messages = [
        {
            "role": "user",
            "content": (
                "这是后台执行阶段的工具 Observation，不是新的用户需求。请继续 ReAct："
                "分析失败原因，必要时调用 update_task_draft 修正结构化 target_body、"
                "response_path 或流式设置，再调用 probe_http_target 实际重试。"
                "不要重复索要用户已经给出的信息；只有确实无法从错误响应和现有配置推断时"
                "才调用 request_user_input。当前 Observation："
                + json.dumps(observation, ensure_ascii=False)
            ),
        }
    ]
    workspace = {
        "current_draft": draft,
        "target_auth_configured": bool(
            agent_config.target_headers or agent_config.target_auth_flows
        ),
        "configured_target_header_names": list(agent_config.target_headers),
        "configured_auth_hosts": list(agent_config.target_auth_flows),
        "available_output_formats": ["xlsx", "jsonl", "markdown", "text"],
        "execution_repair": True,
    }
    active_steps: list[AgentStep] = []
    result: dict[str, Any] | None = None
    for event_type, value in stream_react_configuration(
        agent_config, messages, workspace, job.project_id, job.created_by
    ):
        if event_type == "tool_start":
            active_steps.append(
                _start_react_step(session, job, str(value.get("name", "agent_action")), attempt)
            )
        elif event_type == "tool_result" and active_steps:
            _finish_react_step(session, active_steps.pop(0), value)
        elif event_type == "result":
            result = value
    if result is None:
        return False
    repaired = result.get("draft")
    if not isinstance(repaired, dict) or not repaired.get("target_validated"):
        action = result.get("ui_action")
        if isinstance(action, dict) and action.get("type") == "user_input":
            question = str(action.get("question") or "需要补充目标接口调用信息")
            raise ValueError(f"Agent 需要补充信息：{question}")
        return False
    repaired_body = normalize_target_body(repaired.get("target_body"))
    if not isinstance(repaired_body, (dict, list)):
        return False
    updated_target = {
        **target,
        "url": str(repaired.get("target_url") or target.get("url") or ""),
        "body": repaired_body,
        "response_path": str(repaired.get("response_path") or ""),
    }
    if isinstance(repaired.get("expected_streaming"), bool):
        updated_target["expected_streaming"] = repaired["expected_streaming"]
    job.input_config = {**job.input_config, "target": updated_target}
    job.repair_attempts = attempt
    job.updated_at = datetime.now(UTC)
    session.add(job)
    session.commit()
    return True


def validate_http_target_with_react(
    session: Session, job: AgentJob, agent_config: Any
) -> dict[str, Any]:
    last_error: Exception | None = None
    for attempt in range(1, job.max_repair_attempts + 2):
        try:
            return run_step(
                session,
                job,
                "validate_target",
                lambda: validate_http_target_semantics(session, job, agent_config),
                attempt=attempt,
            )
        except Exception as error:
            last_error = error
            if attempt > job.max_repair_attempts:
                raise
            if not repair_http_target_with_react(
                session, job, agent_config, error, attempt
            ):
                raise
    raise last_error or RuntimeError("Target validation failed")


def validate_http_target_semantics(
    session: Session, job: AgentJob, agent_config: Any
) -> dict[str, Any]:
    preflight = validate_http_target(session, job)
    records = preflight.get("records", [])
    completed = [record for record in records if record.get("status") == "completed"]
    if not completed:
        return preflight
    if not agent_config.enabled or not agent_config.base_url or not agent_config.model:
        preflight["semantic_validation"] = {
            "enabled": False,
            "reason": "未配置可用的评测模型",
        }
        return preflight

    evaluations = run_streamed_model_output(
        job,
        "validate_target",
        "判断预检响应",
        lambda on_delta: evaluate_target_records(
            agent_config,
            job.goal,
            completed,
            evaluation_plan=job.eval_spec,
            on_delta=on_delta,
        ),
    )
    evaluations_by_index = {item["case_index"]: item for item in evaluations}
    for record in completed:
        evaluation = evaluations_by_index.get(record.get("case_index"))
        if evaluation is not None:
            record["evaluation"] = evaluation
    passed = sum(item.get("passed") is True for item in evaluations)
    preflight["semantic_validation"] = {
        "enabled": True,
        "evaluated_cases": len(evaluations),
        "passed_cases": passed,
    }
    if evaluations and passed == 0:
        reasons = "；".join(
            str(item.get("reason") or "响应未满足预期")[:300] for item in evaluations[:3]
        )
        raise ValueError(
            "目标接口语义预检未通过：抽检响应全部不符合测试预期。"
            f"可能是请求体、响应路径或目标服务配置错误。{reasons}"
        )
    return preflight


def plan_agent_job(job_id: UUID) -> None:
    settings = get_settings()
    should_execute = False
    with Session(get_engine()) as session:
        job = session.get(AgentJob, job_id)
        if job is None:
            raise ValueError(f"Agent job not found: {job_id}")
        if job.status not in {AgentJobStatus.PENDING, AgentJobStatus.FAILED}:
            return
        job.error = None
        try:
            initial_steps = ["generate_eval_spec"]
            if job.source_file_id:
                initial_steps.insert(0, "discover_source")
            ensure_pending_steps(session, job, initial_steps)
            agent_config = resolve_agent_config(
                session, job.input_config.get("evaluation_model_id")
            )
            update_job(session, job, AgentJobStatus.DISCOVERING)
            discovery: dict[str, Any] = {"format": None, "fields": [], "samples": []}
            if job.source_file_id:
                file_object = session.get(FileObject, job.source_file_id)
                if file_object is None or file_object.project_id != job.project_id:
                    raise ValueError("Source file does not belong to this project")
                storage = LocalFileStorage(settings.storage.local_directory)
                path = storage.path_for(file_object.storage_key)
                discovery = run_step(
                    session,
                    job,
                    "discover_source",
                    lambda: inspect_source(
                        path, file_object.original_name, settings.agent.dry_run_cases
                    ),
                    {"source_file_id": str(file_object.id)},
                )

            update_job(session, job, AgentJobStatus.PLANNING)
            spec: dict[str, Any] | None = None
            previous_error: str | None = None
            for attempt in range(1, job.max_repair_attempts + 2):
                try:
                    spec = run_step(
                        session,
                        job,
                        "generate_eval_spec",
                        lambda: run_streamed_model_output(
                            job,
                            "generate_eval_spec",
                            "制定评测方案",
                            lambda on_delta, error=previous_error: generate_eval_spec(  # noqa: B023
                                agent_config,
                                job.goal,
                                discovery,
                                job.input_config,
                                error,
                                on_delta=on_delta,
                            ),
                        ),
                        {"goal": job.goal},
                        attempt=attempt,
                    )
                    break
                except Exception as error:
                    if attempt > job.max_repair_attempts:
                        raise
                    previous_error = str(error)[:2000]
                    job.repair_attempts = attempt
                    job.updated_at = datetime.now(UTC)
                    session.add(job)
                    session.commit()
            if spec is None:
                raise RuntimeError("Agent failed to generate EvalSpec")
            target = job.input_config.get("target")
            remaining_steps = []
            if not job.source_file_id and isinstance(target, dict) and target:
                remaining_steps.append("generate_test_cases")
            if isinstance(target, dict) and target:
                remaining_steps.append("validate_target")
            remaining_steps.extend(["execute_eval_spec", "summarize"])
            ensure_pending_steps(session, job, remaining_steps)
            if not job.source_file_id and isinstance(target, dict) and target:
                case_count = int(job.input_config.get("max_cases", 30))
                generated_output = run_step_with_retries(
                    session,
                    job,
                    "generate_test_cases",
                    lambda: {
                        "cases": run_streamed_model_output(
                            job,
                            "generate_test_cases",
                            "生成测试用例",
                            lambda on_delta: generate_test_cases(
                                agent_config,
                                job.goal,
                                job.input_config,
                                spec,
                                case_count,
                                on_delta=on_delta,
                            ),
                        )
                    },
                    {"case_count": case_count},
                )
                generated_cases = generated_output["cases"]
                spec["generated_cases"] = generated_cases
                spec["source"] = {
                    "type": "generated",
                    "format": "json",
                    "case_count": len(generated_cases),
                    "fields": list(generated_cases[0]["input"]),
                }
            job.eval_spec = spec
            job.updated_at = datetime.now(UTC)
            session.add(job)
            session.commit()

            if isinstance(target, dict) and target:
                preflight = validate_http_target_with_react(session, job, agent_config)
                spec["preflight"] = preflight
                job.eval_spec = spec
                job.updated_at = datetime.now(UTC)
                session.add(job)

            job.status = AgentJobStatus.RUNNING
            job.requires_approval = False
            should_execute = True
            session.commit()
        except Exception as error:
            session.rollback()
            job = session.get(AgentJob, job_id)
            if job is not None:
                job.status = AgentJobStatus.FAILED
                job.error = str(error)[:4000]
                job.updated_at = datetime.now(UTC)
                session.add(job)
                session.commit()
                return_error_to_assistant(session, job, error)
            return

    if should_execute:
        execute_agent_job(job_id)


def execute_agent_job(job_id: UUID) -> None:
    with Session(get_engine()) as session:
        job = session.get(AgentJob, job_id)
        if job is None:
            raise ValueError(f"Agent job not found: {job_id}")
        if job.status in {AgentJobStatus.COMPLETED, AgentJobStatus.CANCELLED}:
            return
        try:
            update_job(session, job, AgentJobStatus.RUNNING)
            target = job.input_config.get("target")
            stored_preflight = job.eval_spec.get("preflight")
            preflight = stored_preflight if isinstance(stored_preflight, dict) else None
            if isinstance(target, dict) and target and preflight is None:
                agent_config = resolve_agent_config(
                    session, job.input_config.get("evaluation_model_id")
                )
                preflight = validate_http_target_with_react(session, job, agent_config)

            def execute_spec() -> dict[str, Any]:
                operations = job.eval_spec.get("operations", [])
                source = job.eval_spec.get("source") or job.eval_spec.get("discovery", {})
                if isinstance(target, dict) and target:
                    execution_mode = "target_api"
                    evaluation_config = resolve_agent_config(
                        session, job.input_config.get("evaluation_model_id")
                    )
                    evaluate_records = None
                    if (
                        evaluation_config.enabled
                        and evaluation_config.base_url
                        and evaluation_config.model
                    ):
                        def evaluate_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
                            return run_streamed_model_output(
                                job,
                                "evaluate_results",
                                "评估测试结果",
                                lambda on_delta: evaluate_target_records(
                                    evaluation_config,
                                    job.goal,
                                    records,
                                    evaluation_plan=job.eval_spec,
                                    on_delta=on_delta,
                                ),
                            )
                    execution_result = execute_http_target(
                        session,
                        job,
                        preflight,
                        evaluate_records=evaluate_records,
                    )
                elif isinstance(job.eval_spec.get("data_program"), dict):
                    execution_mode = "data_program"
                    evaluation_config = resolve_agent_config(
                        session, job.input_config.get("evaluation_model_id")
                    )

                    def map_rows(
                        instruction: str,
                        rows: list[dict[str, Any]],
                        output_columns: list[dict[str, Any]],
                        step_index: int,
                    ) -> list[dict[str, Any]]:
                        return run_streamed_model_output(
                            job,
                            f"data_program_{step_index}",
                            f"处理数据 · 步骤 {step_index + 1}",
                            lambda on_delta: model_map_rows(
                                evaluation_config,
                                instruction,
                                rows,
                                output_columns,
                                on_delta=on_delta,
                            ),
                        )

                    execution_result = execute_data_program(session, job, map_rows)
                else:
                    execution_mode = "local_data"
                    execution_result = execute_local_source(session, job)
                result = {
                    "execution_mode": execution_mode,
                    "operations": operations,
                    "source": source,
                    "execution": execution_result,
                }
                result["target" if execution_mode == "target_api" else "dataset"] = execution_result
                return result

            execution = run_step(session, job, "execute_eval_spec", execute_spec)
            update_job(session, job, AgentJobStatus.ANALYZING)

            def summarize_execution() -> dict[str, Any]:
                agent_config = resolve_agent_config(
                    session, job.input_config.get("evaluation_model_id")
                )
                summary = run_streamed_model_output(
                    job,
                    "summarize",
                    "总结评测结果",
                    lambda on_delta: generate_summary(
                        agent_config,
                        job.goal,
                        execution,
                        on_delta=on_delta,
                    ),
                )
                summary["execution"] = execution
                return summary

            result = run_step_with_retries(
                session,
                job,
                "summarize",
                summarize_execution,
            )
            job.result = result
            job.error = None
            update_job(session, job, AgentJobStatus.COMPLETED)
            return_result_to_assistant(session, job, result)
            try:
                notify_agent_job_completed(session, job)
            except Exception:
                logger.exception("Failed to send completion email for agent job %s", job.id)
        except Exception as error:
            session.rollback()
            job = session.get(AgentJob, job_id)
            if job is not None:
                job.status = AgentJobStatus.FAILED
                job.error = str(error)[:4000]
                job.updated_at = datetime.now(UTC)
                session.add(job)
                session.commit()
                return_error_to_assistant(session, job, error)
