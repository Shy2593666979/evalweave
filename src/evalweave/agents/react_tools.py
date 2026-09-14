from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
from sqlmodel import Session

from evalweave.agents.executor import (
    authenticate_target,
    detect_application_error,
    normalize_target_body,
    parse_target_response,
    validate_target,
)
from evalweave.agents.inspection import inspect_source
from evalweave.agents.model_config import encrypt_secret_payload
from evalweave.agents.python_workspace import run_python_workspace
from evalweave.core.config import TargetAuthFlowConfig, get_settings
from evalweave.db.models import FileObject
from evalweave.storage import LocalFileStorage

ASSISTANT_TOOLS = [
    {
        "name": "inspect_source",
        "description": (
            "Inspect an uploaded CSV, JSON, JSONL, or XLSX file before deciding how to use it."
        ),
        "arguments": {"source_file_id": "UUID of the uploaded file"},
    },
    {
        "name": "probe_http_target",
        "description": (
            "Send one concrete request to an HTTP target, using any administrator-configured "
            "authentication flow for its host, and observe status, JSON/SSE mode, and "
            "response shape."
        ),
        "arguments": {
            "url": "HTTP target URL",
            "body": "one complete representative JSON request without template placeholders",
        },
    },
]


@dataclass
class AssistantToolContext:
    draft: dict[str, Any]
    project_id: UUID | None
    ui_action: dict[str, Any] | None = None
    trace: list[dict[str, Any]] = field(default_factory=list)
    reviewer_type_counts: dict[str, int] | None = None
    available_reviewer_usernames: set[str] | None = None


class BaseAssistantTool(ABC):
    name: str
    description: str
    parameters: dict[str, Any]
    terminal: bool = False

    def can_stream_terminal_text(self, context: AssistantToolContext) -> bool:
        return False

    def to_model_def(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
        }

    @abstractmethod
    def run(self, context: AssistantToolContext, **kwargs: Any) -> str:
        raise NotImplementedError


class UpdateTaskDraftTool(BaseAssistantTool):
    name = "update_task_draft"
    description = "保存从用户需求中确认的评测任务字段。只提交已经明确或可可靠推断的字段。"
    parameters = {
        "type": "object",
        "properties": {
            "task_mode": {
                "type": "string",
                "enum": ["local_analysis", "dataset_target", "generated_target", "human_review"],
            },
            "title": {"type": "string"},
            "goal": {"type": "string"},
            "source_file_id": {"type": "string"},
            "target_url": {"type": "string"},
            "target_body": {"type": ["object", "array"]},
            "response_path": {"type": "string"},
            "expected_streaming": {"type": "boolean"},
            "auth_required": {"type": "boolean"},
            "max_cases": {"type": "integer", "minimum": 1, "maximum": 10000},
            "requires_approval": {"type": "boolean"},
            "output_format": {"type": "string", "enum": ["xlsx", "jsonl", "markdown", "text"]},
            "reviewer_type_codes": {
                "type": "array",
                "description": (
                    "仅保存用户明确要求的全部角色群组；具体人员的角色说明不属于群组选择。"
                ),
                "items": {
                    "type": "string",
                    "enum": ["product", "research", "development", "testing"],
                },
            },
            "reviewer_usernames": {"type": "array", "items": {"type": "string"}},
            "query_column": {"type": "string"},
            "answer_column": {"type": "string"},
            "latency_column": {"type": "string"},
            "deadline_hours": {"type": "number", "exclusiveMinimum": 0, "maximum": 720},
            "review_rubric": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "key": {"type": "string"},
                        "label": {"type": "string"},
                        "min_score": {"type": "number"},
                        "max_score": {"type": "number"},
                    },
                    "required": ["key", "label", "min_score", "max_score"],
                    "additionalProperties": False,
                },
            },
            "targets": {
                "type": "array",
                "maxItems": 8,
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "url": {"type": "string"},
                        "body": {"type": ["object", "array"]},
                        "response_path": {"type": "string"},
                        "answer_column": {"type": "string"},
                        "latency_column": {"type": "string"},
                        "ttfb_column": {"type": "string"},
                    },
                    "required": ["url", "body"],
                    "additionalProperties": False,
                },
            },
        },
        "additionalProperties": False,
    }

    def run(self, context: AssistantToolContext, **kwargs: Any) -> str:
        allowed = set(self.parameters["properties"])
        updates = {
            key: value for key, value in kwargs.items() if key in allowed and value is not None
        }
        context.draft.update(updates)
        _ensure_human_review_defaults(context.draft)
        return json.dumps({"ok": True, "updated_fields": sorted(updates)}, ensure_ascii=False)


class InspectSourceTool(BaseAssistantTool):
    name = "inspect_source"
    description = "检查已上传数据文件的格式、字段和样例。需要理解文件时调用。"
    parameters = {
        "type": "object",
        "properties": {"source_file_id": {"type": "string"}},
        "required": ["source_file_id"],
        "additionalProperties": False,
    }

    def run(self, context: AssistantToolContext, **kwargs: Any) -> str:
        with Session(get_settings_engine()) as session:
            observation, updates = run_assistant_tool(
                self.name, kwargs, context.draft, session, context.project_id
            )
        context.draft.update(updates)
        return json.dumps({"ok": True, **observation}, ensure_ascii=False)


class RunPythonTool(BaseAssistantTool):
    name = "run_python"
    description = (
        "在临时 Python 工作区中处理当前项目文件。输入文件位于 inputs/，manifest.json 描述文件映射；"
        "脚本可使用 Python 标准库和项目已安装依赖，并应把新文件写入 outputs/。"
        "适用于任意清洗、JSON 解包、列转换、合并、拆分或格式修复。用户要求修改文件时直接调用，"
        "不得声称没有脚本或要求用户自行修改后重传。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "code": {"type": "string", "description": "要执行的完整 Python 脚本。"},
            "source_file_ids": {
                "type": "array",
                "items": {"type": "string"},
                "description": "输入文件 ID；省略时使用当前 source_file_id。",
                "maxItems": 8,
            },
            "primary_output": {
                "type": "string",
                "description": "outputs/ 下作为后续任务数据源的文件名；默认使用第一个输出。",
            },
        },
        "required": ["code"],
        "additionalProperties": False,
    }

    def run(self, context: AssistantToolContext, **kwargs: Any) -> str:
        with Session(get_settings_engine()) as session:
            observation, updates = run_python_workspace(
                session,
                context.project_id,
                context.draft,
                str(kwargs.get("code", "")),
                [str(item) for item in kwargs.get("source_file_ids", [])] or None,
                str(kwargs.get("primary_output") or "") or None,
            )
        context.draft.update(updates)
        return json.dumps({"ok": True, **observation}, ensure_ascii=False)


class ProbeHttpTargetTool(BaseAssistantTool):
    name = "probe_http_target"
    description = (
        "实际请求目标 HTTP 接口并观察状态、JSON/SSE 响应和响应结构。"
        "工具可直接使用任务请求头或先执行登录流程，再自动注入 Token/Cookie。"
        "有地址和可发送请求体后必须调用本工具，不能让用户改用 curl/Postman 代替。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "url": {"type": "string"},
            "body": {"type": ["object", "array"]},
            "headers": {
                "type": "object",
                "description": "用户为本任务提供的请求头；可包含 Authorization。",
                "additionalProperties": {"type": "string"},
            },
            "auth": {
                "type": "object",
                "description": "可选登录流程。工具先登录，再用提取的 Token 或 Cookie 请求目标。",
                "properties": {
                    "login_url": {"type": "string"},
                    "body": {"type": "object"},
                    "token_path": {"type": "string"},
                    "header_name": {"type": "string"},
                    "header_prefix": {"type": "string"},
                },
                "required": ["login_url", "body"],
                "additionalProperties": False,
            },
        },
        "required": ["url", "body"],
        "additionalProperties": False,
    }

    def run(self, context: AssistantToolContext, **kwargs: Any) -> str:
        with Session(get_settings_engine()) as session:
            observation, updates = run_assistant_tool(
                self.name, kwargs, context.draft, session, context.project_id
            )
        context.draft.update(updates)
        return json.dumps({"ok": True, **observation}, ensure_ascii=False)


class RequestOutputFormatTool(BaseAssistantTool):
    name = "request_output_format"
    description = "当任务信息和必要检查已完成但尚未选择交付格式时，显示格式选择按钮。"
    parameters = {"type": "object", "properties": {}, "additionalProperties": False}
    terminal = True

    def run(self, context: AssistantToolContext, **kwargs: Any) -> str:
        if not _draft_checked(context.draft):
            return json.dumps(
                {"ok": False, "error": "任务尚未完成必要的文件检查或接口预检"},
                ensure_ascii=False,
            )
        context.ui_action = {"type": "choose_output"}
        return json.dumps({"ok": True, "waiting_for": "output_format"}, ensure_ascii=False)


class RequestConfirmationTool(BaseAssistantTool):
    name = "request_confirmation"
    description = "任务已准备好时，生成针对本任务的确认摘要并显示确认执行按钮。"
    parameters = {
        "type": "object",
        "properties": {
            "summary": {"type": "string", "description": "基于当前任务实际配置生成的中文确认摘要"}
        },
        "required": ["summary"],
        "additionalProperties": False,
    }
    terminal = True

    def can_stream_terminal_text(self, context: AssistantToolContext) -> bool:
        _ensure_human_review_defaults(context.draft)
        return bool(
            _draft_checked(context.draft)
            and context.draft.get("output_format")
            and not _reviewer_availability_error(context)
        )

    def run(self, context: AssistantToolContext, **kwargs: Any) -> str:
        _ensure_human_review_defaults(context.draft)
        if not _draft_checked(context.draft) or not context.draft.get("output_format"):
            return json.dumps(
                {"ok": False, "error": "任务尚未验证完成或未选择输出格式"},
                ensure_ascii=False,
            )
        reviewer_error = _reviewer_availability_error(context)
        if reviewer_error:
            return json.dumps({"ok": False, "error": reviewer_error}, ensure_ascii=False)
        summary = str(kwargs.get("summary", "")).replace("\\n", "\n").strip()
        context.ui_action = {"type": "confirm", "summary": summary}
        return json.dumps({"ok": True, "waiting_for": "confirmation"}, ensure_ascii=False)


class RequestUserInputTool(BaseAssistantTool):
    name = "request_user_input"
    description = "只有缺少无法通过工具获得的必要信息时，向用户提出一个具体问题。"
    parameters = {
        "type": "object",
        "properties": {
            "question": {"type": "string"},
            "options": {"type": "array", "items": {"type": "string"}, "maxItems": 4},
        },
        "required": ["question"],
        "additionalProperties": False,
    }
    terminal = True

    def can_stream_terminal_text(self, context: AssistantToolContext) -> bool:
        return True

    def run(self, context: AssistantToolContext, **kwargs: Any) -> str:
        question = str(kwargs.get("question", "")).strip()
        options = [str(item) for item in kwargs.get("options", [])]
        normalized_options = {item.strip().casefold() for item in options}
        if normalized_options == {"xlsx", "jsonl", "markdown", "text"}:
            context.ui_action = {"type": "choose_output", "question": question}
            return json.dumps(
                {"ok": True, "waiting_for": "output_format"}, ensure_ascii=False
            )
        context.ui_action = {
            "type": "user_input",
            "question": question,
            "options": options,
        }
        return json.dumps({"ok": True, "waiting_for": "user_input"}, ensure_ascii=False)


ASSISTANT_TOOL_REGISTRY: list[BaseAssistantTool] = [
    UpdateTaskDraftTool(),
    InspectSourceTool(),
    RunPythonTool(),
    ProbeHttpTargetTool(),
    RequestOutputFormatTool(),
    RequestConfirmationTool(),
    RequestUserInputTool(),
]


def _reviewer_availability_error(context: AssistantToolContext) -> str | None:
    if context.draft.get("task_mode") != "human_review":
        return None
    if context.reviewer_type_counts is None or context.available_reviewer_usernames is None:
        return None
    requested_types = {
        str(item).strip().lower()
        for item in context.draft.get("reviewer_type_codes", [])
        if str(item).strip()
    }
    requested_names = {
        str(item).strip().lower()
        for item in context.draft.get("reviewer_usernames", [])
        if str(item).strip()
    }
    empty_types = sorted(
        code for code in requested_types if context.reviewer_type_counts.get(code, 0) <= 0
    )
    missing_names = sorted(requested_names - context.available_reviewer_usernames)
    problems: list[str] = []
    if empty_types:
        problems.append(f"这些评审人员类型当前没有可用用户：{', '.join(empty_types)}")
    if missing_names:
        problems.append(f"这些指定评审人员不存在、已停用或没有评审权限：{', '.join(missing_names)}")
    if problems:
        return "；".join(problems) + "。请重新选择评审人员后再请求确认。"
    return None


def _ensure_human_review_defaults(draft: dict[str, Any]) -> None:
    if draft.get("task_mode") != "human_review":
        return
    draft["output_format"] = "text"
    if str(draft.get("goal", "")).strip():
        return
    rubric = draft.get("review_rubric")
    labels = [
        str(item.get("label", "")).strip()
        for item in rubric
        if isinstance(item, dict) and str(item.get("label", "")).strip()
    ] if isinstance(rubric, list) else []
    dimensions = "、".join(labels) or "已配置的评分"
    draft["goal"] = f"由指定评审人员按{dimensions}维度对文件中的回答进行人工评分。"


def _draft_checked(draft: dict[str, Any]) -> bool:
    source_ready = bool(draft.get("source_file_id") and draft.get("source_inspected"))
    if draft.get("task_mode") == "human_review":
        return bool(
            draft.get("title")
            and draft.get("goal")
            and source_ready
            and (draft.get("reviewer_type_codes") or draft.get("reviewer_usernames"))
            and draft.get("review_rubric")
            and draft.get("deadline_hours")
        )
    targets = draft.get("targets")
    if isinstance(targets, list) and targets:
        configured_urls = {
            str(target.get("url", "")).strip()
            for target in targets
            if isinstance(target, dict) and target.get("url")
        }
        validated_urls = {str(url) for url in draft.get("validated_targets", [])}
        source_ready = source_ready and bool(configured_urls) and configured_urls <= validated_urls
    target_ready = bool(
        draft.get("target_url") and draft.get("target_body") and draft.get("target_validated")
    )
    return bool(draft.get("title") and draft.get("goal") and (source_ready or target_ready))


def get_settings_engine():
    from evalweave.db.session import get_engine

    return get_engine()


def _probe_value(name: str) -> str:
    lowered = name.lower()
    if any(part in lowered for part in ("query", "prompt", "message", "question", "text")):
        return "请用一句话说明 1+1 等于多少。"
    if "session" in lowered:
        return uuid4().hex
    if lowered.endswith("id") or lowered.endswith("_id"):
        return "test"
    return "test"


def materialize_probe_body(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: materialize_probe_body(item) for key, item in value.items()}
    if isinstance(value, list):
        return [materialize_probe_body(item) for item in value]
    if not isinstance(value, str):
        return value
    full_placeholder = re.fullmatch(r"\{\{\s*([^{}]+?)\s*\}\}", value)
    if full_placeholder:
        return _probe_value(full_placeholder.group(1))
    return re.sub(
        r"\{\{\s*([^{}]+?)\s*\}\}",
        lambda match: _probe_value(match.group(1)),
        value,
    )


def _safe_sample(value: Any, limit: int = 1800) -> Any:
    serialized = json.dumps(value, ensure_ascii=False)
    if len(serialized) <= limit:
        return value
    return serialized[:limit] + "..."


def run_assistant_tool(
    name: str,
    arguments: dict[str, Any],
    draft: dict[str, Any],
    session: Session,
    project_id: UUID | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    if name == "inspect_source":
        raw_file_id = arguments.get("source_file_id") or draft.get("source_file_id")
        if not raw_file_id:
            raise ValueError("inspect_source 需要 source_file_id")
        file_object = session.get(FileObject, UUID(str(raw_file_id)))
        if file_object is None or (project_id and file_object.project_id != project_id):
            raise ValueError("数据文件不存在或不属于当前项目")
        path: Path = LocalFileStorage(get_settings().storage.local_directory).path_for(
            file_object.storage_key
        )
        observation = inspect_source(
            path,
            file_object.original_name,
            get_settings().agent.dry_run_cases,
        )
        return (
            {
                "file_name": file_object.original_name,
                "format": observation.get("format"),
                "fields": observation.get("fields", []),
                "sample_count": len(observation.get("samples", [])),
            },
            {
                "source_inspected": True,
                "source_fields": observation.get("fields", []),
                "source_file_name": file_object.original_name,
            },
        )

    if name == "probe_http_target":
        url = str(arguments.get("url") or draft.get("target_url") or "").strip()
        template = normalize_target_body(draft.get("target_body"))
        body = arguments.get("body")
        if not isinstance(body, (dict, list)):
            body = materialize_probe_body(template)
        if not isinstance(body, (dict, list)):
            raise ValueError("probe_http_target 需要可发送的 JSON 请求体")
        task_headers = arguments.get("headers")
        if task_headers is not None and not isinstance(task_headers, dict):
            raise ValueError("probe_http_target headers 必须是对象")
        raw_auth = arguments.get("auth")
        if raw_auth is not None and not isinstance(raw_auth, dict):
            raise ValueError("probe_http_target auth 必须是对象")
        auth = TargetAuthFlowConfig.model_validate(raw_auth).model_dump() if raw_auth else None
        credentials = draft.get("target_credentials")
        if task_headers or auth:
            credentials = encrypt_secret_payload(
                {
                    "headers": {
                        str(key): str(value) for key, value in (task_headers or {}).items()
                    },
                    "auth": auth,
                }
            )
        target_config = {"url": url}
        if isinstance(credentials, str) and credentials:
            target_config["credentials"] = credentials
        _, headers = validate_target(target_config)
        timeout = float(get_settings().evaluation.default_timeout_seconds)
        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            authentication = authenticate_target(client, url, headers, credentials)
            try:
                response = client.post(url, headers=headers, json=body)
                response.raise_for_status()
                application_error = detect_application_error(response)
                if application_error:
                    raise ValueError(f"目标接口返回业务错误：{application_error}")
            except Exception as error:
                failed_response = getattr(error, "response", None)
                response_text = str(getattr(failed_response, "text", "")).strip()
                detail = f"；响应体：{response_text[:2000]}" if response_text else ""
                raise ValueError(f"HTTP 预检失败：{error}{detail}") from error
            output, response_mode = parse_target_response(response, "")
        return (
            {
                "status_code": response.status_code,
                "response_mode": response_mode,
                "content_type": response.headers.get("content-type", ""),
                "response_sample": _safe_sample(output),
                "authentication": authentication,
            },
            {
                "target_url": url,
                "target_body": template,
                "target_validated": True,
                "validated_targets": list(
                    dict.fromkeys([*draft.get("validated_targets", []), url])
                ),
                "expected_streaming": response_mode == "streaming",
                **({"target_credentials": credentials} if credentials else {}),
            },
        )

    raise ValueError(f"未知 Agent 工具：{name}")


def tool_label(name: str) -> str:
    return {
        "update_task_draft": "整理任务信息",
        "inspect_source": "检查数据文件",
        "run_python": "运行 Python 文件处理",
        "probe_http_target": "验证目标接口",
        "request_output_format": "请求选择交付方式",
        "request_confirmation": "请求确认任务",
        "request_user_input": "请求补充信息",
    }.get(name, name)
