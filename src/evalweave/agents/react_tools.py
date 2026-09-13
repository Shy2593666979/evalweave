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
    normalize_target_body,
    parse_target_response,
    validate_target,
)
from evalweave.agents.inspection import inspect_source
from evalweave.core.config import get_settings
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


class BaseAssistantTool(ABC):
    name: str
    description: str
    parameters: dict[str, Any]
    terminal: bool = False

    def to_openai_def(self, api_mode: str) -> dict[str, Any]:
        definition = {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
        }
        if api_mode == "responses":
            return {"type": "function", **definition}
        return {"type": "function", "function": definition}

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
                "description": "仅保存用户明确要求的全部角色群组；具体人员的角色说明不属于群组选择。",
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


class ProbeHttpTargetTool(BaseAssistantTool):
    name = "probe_http_target"
    description = (
        "实际请求目标 HTTP 接口并观察状态、JSON/SSE 响应和响应结构。"
        "有地址和可发送请求体后必须先调用本工具，不能让用户代替提供响应。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "url": {"type": "string"},
            "body": {"type": ["object", "array"]},
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

    def run(self, context: AssistantToolContext, **kwargs: Any) -> str:
        _ensure_human_review_defaults(context.draft)
        if not _draft_checked(context.draft) or not context.draft.get("output_format"):
            return json.dumps(
                {"ok": False, "error": "任务尚未验证完成或未选择输出格式"},
                ensure_ascii=False,
            )
        summary = str(kwargs.get("summary", "")).strip()
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

    def run(self, context: AssistantToolContext, **kwargs: Any) -> str:
        context.ui_action = {
            "type": "user_input",
            "question": str(kwargs.get("question", "")).strip(),
            "options": [str(item) for item in kwargs.get("options", [])],
        }
        return json.dumps({"ok": True, "waiting_for": "user_input"}, ensure_ascii=False)


ASSISTANT_TOOL_REGISTRY: list[BaseAssistantTool] = [
    UpdateTaskDraftTool(),
    InspectSourceTool(),
    ProbeHttpTargetTool(),
    RequestOutputFormatTool(),
    RequestConfirmationTool(),
    RequestUserInputTool(),
]


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
        _, headers = validate_target({"url": url})
        timeout = float(get_settings().evaluation.default_timeout_seconds)
        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            authentication = authenticate_target(client, url, headers)
            try:
                response = client.post(url, headers=headers, json=body)
                response.raise_for_status()
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
            },
        )

    raise ValueError(f"未知 Agent 工具：{name}")


def tool_label(name: str) -> str:
    return {
        "update_task_draft": "整理任务信息",
        "inspect_source": "检查数据文件",
        "probe_http_target": "验证目标接口",
        "request_output_format": "请求选择交付方式",
        "request_confirmation": "请求确认任务",
        "request_user_input": "请求补充信息",
    }.get(name, name)
