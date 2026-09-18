from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterator
from typing import Any

from evalweave.agents.model_client import AgentModelClient
from evalweave.agents.prompts import (
    ASSISTANT_PROMPT,
    CASE_EVALUATION_PROMPT,
    DATA_MODEL_MAP_PROMPT,
    PLANNING_SYSTEM_PROMPT,
    PYTHON_REPAIR_PROMPT,
    TEST_CASE_PROMPT,
)
from evalweave.core.config import AgentConfig


def derive_task_title(goal: str) -> str:
    compact = re.sub(r"\s+", " ", goal).strip()
    compact = re.sub(r"^(?:请|帮我|我要|我想|需要|希望)+", "", compact).strip()
    compact = compact.strip("，。！？；：,.!?;: ")
    return compact[:32].rstrip("，。！？；：,.!?;: ") or "智能评测任务"


def request_json(
    config: AgentConfig,
    system_prompt: str,
    payload: dict[str, Any],
    on_delta: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    serialized = json.dumps(payload, ensure_ascii=False)
    content = AgentModelClient(config).request_text(
        system_prompt,
        [{"role": "user", "content": serialized}],
        json_mode=True,
        on_delta=on_delta,
    )
    if not content:
        raise ValueError("Agent model returned empty content")
    result = json.loads(content)
    if not isinstance(result, dict):
        raise ValueError("Agent model must return a JSON object")
    return result


def request_text(
    config: AgentConfig,
    system_prompt: str,
    payload: dict[str, Any],
    on_delta: Callable[[str], None] | None = None,
) -> str:
    serialized = json.dumps(payload, ensure_ascii=False)
    content = AgentModelClient(config).request_text(
        system_prompt,
        [{"role": "user", "content": serialized}],
        on_delta=on_delta,
    )
    content = content.strip()
    if not content:
        raise ValueError("Agent model returned empty summary")
    return content


def repair_python_script(
    config: AgentConfig,
    goal: str,
    code: str,
    error: str,
) -> str:
    result = request_json(
        config,
        PYTHON_REPAIR_PROMPT,
        {
            "goal": goal,
            "code": code,
            "error": error[-6000:],
        },
    )
    repaired = str(result.get("code") or "").strip()
    if not repaired:
        raise ValueError("模型没有返回修复后的 Python 脚本")
    return repaired


def extract_partial_json_string(document: str, key: str) -> str:
    match = re.search(rf'"{re.escape(key)}"\s*:\s*"', document)
    if match is None:
        return ""
    start = match.end()
    escaped = False
    end = len(document)
    for index in range(start, len(document)):
        character = document[index]
        if escaped:
            escaped = False
            continue
        if character == "\\":
            escaped = True
            continue
        if character == '"':
            end = index
            break
    raw = document[start:end]
    if raw.endswith("\\"):
        raw = raw[:-1]
    try:
        return json.loads(f'"{raw}"')
    except json.JSONDecodeError:
        return raw


def stream_json(
    config: AgentConfig, system_prompt: str, payload: dict[str, Any]
) -> Iterator[tuple[str, Any]]:
    serialized = json.dumps(payload, ensure_ascii=False)
    content = ""
    emitted_reply = ""
    deltas = AgentModelClient(config).stream_text(
        system_prompt,
        [{"role": "user", "content": serialized}],
        json_mode=True,
    )
    for delta in deltas:
        content += delta
        partial_reply = extract_partial_json_string(content, "reply")
        if partial_reply.startswith(emitted_reply) and len(partial_reply) > len(emitted_reply):
            reply_delta = partial_reply[len(emitted_reply) :]
            emitted_reply = partial_reply
            yield "delta", reply_delta
    if not content:
        raise ValueError("Agent model returned empty content")
    result = json.loads(content)
    if not isinstance(result, dict):
        raise ValueError("Agent model must return a JSON object")
    final_reply = result.get("reply")
    if isinstance(final_reply, str) and final_reply.startswith(emitted_reply):
        remainder = final_reply[len(emitted_reply) :]
        if remainder:
            yield "delta", remainder
    yield "result", result


def filter_assistant_result(result: dict[str, Any]) -> dict[str, Any]:
    reply = result.get("reply")
    draft = result.get("draft", {})
    if not isinstance(reply, str) or not isinstance(draft, dict):
        raise ValueError("Agent assistant returned an invalid response")
    allowed_fields = {
        "title",
        "goal",
        "task_mode",
        "source_file_id",
        "target_url",
        "target_body",
        "response_path",
        "expected_streaming",
        "target_validated",
        "source_inspected",
        "target_auth_id",
        "auth_required",
        "max_cases",
        "output_format",
        "targets",
        "validated_targets",
    }
    filtered_draft = {key: value for key, value in draft.items() if key in allowed_fields}
    target_body = filtered_draft.get("target_body")
    if isinstance(target_body, str):
        try:
            decoded_body = json.loads(target_body)
        except json.JSONDecodeError:
            pass
        else:
            if isinstance(decoded_body, (dict, list)):
                filtered_draft["target_body"] = decoded_body
    tool_call = result.get("tool_call")
    if tool_call is not None and not isinstance(tool_call, dict):
        raise ValueError("Agent assistant returned an invalid tool call")
    return {"reply": reply, "draft": filtered_draft, "tool_call": tool_call}


def stream_assist_job_configuration(
    config: AgentConfig,
    messages: list[dict[str, str]],
    context: dict[str, Any],
) -> Iterator[tuple[str, Any]]:
    if not config.enabled or not config.base_url or not config.model:
        result = assist_job_configuration(config, messages, context)
        yield "delta", result["reply"]
        yield "result", result
        return
    for event_type, value in stream_json(
        config,
        ASSISTANT_PROMPT,
        {"conversation": messages[-12:], "workspace": context},
    ):
        if event_type == "delta":
            yield event_type, value
        else:
            yield "result", filter_assistant_result(value)


def build_fallback_spec(goal: str, discovery: dict[str, Any]) -> dict[str, Any]:
    return {
        "version": 1,
        "goal": goal,
        "source": {
            "format": discovery.get("format"),
            "fields": discovery.get("fields", []),
        },
        "operations": [
            "normalize",
            "target_call",
            "conversation_eval",
            "tool_eval",
            "latency_eval",
            "safety_eval",
            "human_review",
            "summarize",
        ],
        "mapping": {},
        "target": {},
        "evaluators": {
            "conversation": ["empathy", "continuity", "persona_consistency", "naturalness"],
            "tools": ["selection", "arguments", "result_grounding"],
            "latency": ["ttft_ms", "total_ms", "tool_ms"],
            "safety": ["emotional_reliance", "self_harm", "delusion", "minor_safety"],
        },
        "needs_configuration": ["mapping", "target"],
        "discovery": discovery,
    }


def generate_eval_spec(
    config: AgentConfig,
    goal: str,
    discovery: dict[str, Any],
    input_config: dict[str, Any],
    previous_error: str | None = None,
    on_delta: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    if not config.enabled or not config.base_url or not config.model:
        return build_fallback_spec(goal, discovery)
    request_options = {"on_delta": on_delta} if on_delta is not None else {}
    spec = request_json(
        config,
        PLANNING_SYSTEM_PROMPT,
        {
            "goal": goal,
            "source_discovery": discovery,
            "input_config": input_config,
            "previous_error": previous_error,
        },
        **request_options,
    )
    operations = spec.get("operations", [])
    if not operations and isinstance(spec.get("plan"), list):
        operations = [
            item.get("operation")
            for item in spec["plan"]
            if isinstance(item, dict) and isinstance(item.get("operation"), str)
        ]
        spec["operations"] = operations
    allowed = {
        "normalize",
        "data_analysis",
        "ranking",
        "aggregate",
        "data_transform",
        "format_convert",
        "target_call",
        "multi_target_call",
        "conversation_eval",
        "tool_eval",
        "latency_eval",
        "safety_eval",
        "human_review",
        "summarize",
    }
    # Model providers do not always follow the exact operation vocabulary even when
    # JSON mode is enabled. Keep any supported names and use a deterministic local
    # data pipeline for uploaded-file jobs instead of failing the whole run.
    if not isinstance(operations, list):
        operations = []
    operations = [item for item in operations if isinstance(item, str) and item in allowed]
    has_target = (
        isinstance(input_config.get("target"), dict) and bool(input_config["target"])
    ) or (isinstance(input_config.get("targets"), list) and bool(input_config["targets"]))
    if has_target:
        if "target_call" not in operations:
            operations.insert(0, "target_call")
    else:
        local_operations = ["normalize", "data_analysis"]
        if any(keyword in goal for keyword in ("排名", "排行", "比拼", "最高", "最低")):
            local_operations.append("ranking")
        operations = list(dict.fromkeys([*local_operations, *operations]))
        operations = [item for item in operations if item != "target_call"]
    if "summarize" not in operations:
        operations.append("summarize")
    spec["operations"] = operations
    spec.setdefault("source", {key: value for key, value in discovery.items() if key != "samples"})
    spec["discovery"] = discovery
    return spec


def generate_test_cases(
    config: AgentConfig,
    goal: str,
    input_config: dict[str, Any],
    eval_spec: dict[str, Any],
    count: int,
    on_delta: Callable[[str], None] | None = None,
) -> list[dict[str, Any]]:
    if not config.enabled or not config.base_url or not config.model:
        raise ValueError("AI-generated test cases require an enabled evaluation model")
    if not 1 <= count <= 200:
        raise ValueError("AI-generated test case count must be between 1 and 200")
    request_options = {"on_delta": on_delta} if on_delta is not None else {}
    result = request_json(
        config,
        TEST_CASE_PROMPT,
        {
            "goal": goal,
            "requested_case_count": count,
            "target": input_config.get("target", {}),
            "evaluation_plan": eval_spec,
        },
        **request_options,
    )
    cases = result.get("cases")
    if not isinstance(cases, list) or len(cases) != count:
        actual = len(cases) if isinstance(cases, list) else 0
        raise ValueError(f"Evaluation model generated {actual} cases; expected {count}")
    if not all(
        isinstance(case, dict) and isinstance(case.get("input"), dict) and case["input"]
        for case in cases
    ):
        raise ValueError("Every AI-generated test case must contain a non-empty input object")
    return cases


def evaluate_target_records(
    config: AgentConfig,
    goal: str,
    records: list[dict[str, Any]],
    evaluation_plan: dict[str, Any] | None = None,
    on_delta: Callable[[str], None] | None = None,
) -> list[dict[str, Any]]:
    completed = [record for record in records if record.get("status") == "completed"]
    if not completed:
        return []
    if not config.enabled or not config.base_url or not config.model:
        raise ValueError("Target response scoring requires an enabled evaluation model")

    cases = []
    for record in completed:
        output = record.get("output")
        serialized_output = json.dumps(output, ensure_ascii=False)
        if len(serialized_output) > 6000:
            serialized_output = serialized_output[:6000] + "...[内容已截断用于评分]"
        cases.append(
            {
                "case_index": record.get("case_index"),
                "input": record.get("input"),
                "expected": (record.get("input") or {}).get("expected")
                if isinstance(record.get("input"), dict)
                else {},
                "actual_output": serialized_output,
                "latency_ms": record.get("latency_ms"),
                "ttfb_ms": record.get("ttfb_ms"),
                "response_mode": record.get("response_mode"),
            }
        )

    request_options = {"on_delta": on_delta} if on_delta is not None else {}
    result = request_json(
        config,
        CASE_EVALUATION_PROMPT,
        {
            "evaluation_goal": goal,
            "evaluation_plan": {
                "operations": (evaluation_plan or {}).get("operations", []),
                "evaluators": (evaluation_plan or {}).get("evaluators", {}),
            },
            "cases": cases,
        },
        **request_options,
    )
    evaluations = result.get("evaluations")
    if not isinstance(evaluations, list):
        raise ValueError("Evaluation model did not return an evaluations array")

    normalized: list[dict[str, Any]] = []
    expected_indexes = {record.get("case_index") for record in completed}
    for evaluation in evaluations:
        if not isinstance(evaluation, dict) or evaluation.get("case_index") not in expected_indexes:
            continue
        raw_dimensions = evaluation.get("dimensions")
        if not isinstance(raw_dimensions, list) or not raw_dimensions:
            raise ValueError("Evaluation model returned no scoring dimensions")
        dimensions: list[dict[str, Any]] = []
        seen_keys: set[str] = set()
        for position, dimension in enumerate(raw_dimensions):
            if not isinstance(dimension, dict):
                continue
            key = re.sub(r"[^a-z0-9_]+", "_", str(dimension.get("key", "")).lower()).strip("_")
            if not key:
                key = f"dimension_{position + 1}"
            if key in seen_keys:
                continue
            try:
                score = round(min(10.0, max(1.0, float(dimension.get("score")))), 2)
            except (TypeError, ValueError):
                raise ValueError(f"Evaluation model returned an invalid score for {key}") from None
            seen_keys.add(key)
            dimensions.append(
                {
                    "key": key,
                    "label": str(dimension.get("label") or key).strip()[:64],
                    "score": score,
                    "reason": str(dimension.get("reason", "")).strip()[:1000],
                }
            )
        if not dimensions:
            raise ValueError("Evaluation model returned no valid scoring dimensions")
        try:
            overall_score = round(
                min(10.0, max(1.0, float(evaluation.get("overall_score")))), 2
            )
        except (TypeError, ValueError):
            overall_score = round(
                sum(float(item["score"]) for item in dimensions) / len(dimensions), 2
            )
        passed = evaluation.get("passed")
        if not isinstance(passed, bool):
            passed = overall_score >= 6
        normalized.append(
            {
                "case_index": evaluation["case_index"],
                "dimensions": dimensions,
                "overall_score": overall_score,
                "passed": passed,
                "reason": str(evaluation.get("reason", "")).strip()[:2000],
            }
        )
    if {item["case_index"] for item in normalized} != expected_indexes:
        raise ValueError("Evaluation model did not score every completed case")
    return normalized


def model_map_rows(
    config: AgentConfig,
    instruction: str,
    rows: list[dict[str, Any]],
    output_columns: list[dict[str, Any]],
    on_delta: Callable[[str], None] | None = None,
) -> list[dict[str, Any]]:
    if not config.enabled or not config.base_url or not config.model:
        raise ValueError("Generic model transformation requires an enabled evaluation model")
    if not instruction.strip():
        raise ValueError("Generic model transformation requires an instruction")
    if not output_columns:
        raise ValueError("Generic model transformation requires output columns")
    normalized_columns = []
    for column in output_columns[:20]:
        if not isinstance(column, dict) or not str(column.get("name", "")).strip():
            raise ValueError("Every model output column requires a name")
        column_type = str(column.get("type", "string"))
        if column_type not in {"string", "number", "boolean", "array"}:
            raise ValueError(f"Unsupported model output column type: {column_type}")
        normalized_columns.append({"name": str(column["name"]).strip(), "type": column_type})

    transformed: list[dict[str, Any]] = []
    batch_size = 20
    for offset in range(0, len(rows), batch_size):
        batch = rows[offset : offset + batch_size]
        request_options = {"on_delta": on_delta} if on_delta is not None else {}
        result = request_json(
            config,
            DATA_MODEL_MAP_PROMPT,
            {
                "instruction": instruction,
                "output_columns": normalized_columns,
                "rows": [
                    {"row_index": offset + index, "data": row}
                    for index, row in enumerate(batch)
                ],
            },
            **request_options,
        )
        items = result.get("results")
        if not isinstance(items, list) or len(items) != len(batch):
            raise ValueError("Model transformation did not return one result for every row")
        by_index = {
            int(item["row_index"]): item.get("values")
            for item in items
            if isinstance(item, dict)
            and isinstance(item.get("row_index"), int)
            and isinstance(item.get("values"), dict)
        }
        for index in range(offset, offset + len(batch)):
            values = by_index.get(index)
            if values is None:
                raise ValueError(f"Model transformation omitted row {index}")
            transformed.append(
                {column["name"]: values.get(column["name"]) for column in normalized_columns}
            )
    return transformed


def generate_summary(
    config: AgentConfig,
    goal: str,
    execution: dict[str, Any],
    on_delta: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    fallback = {
        "summary": "EvalSpec 已验证，已执行允许的内置连接器。",
        "findings": [execution],
        "recommendations": [],
    }
    if not config.enabled or not config.base_url or not config.model:
        return fallback
    request_options = {"on_delta": on_delta} if on_delta is not None else {}
    summary = request_text(
        config,
        (
            "Write the final user-facing reply for the completed evaluation in natural Chinese "
            "plain text or concise Markdown. State the outcome first, then synthesize the most "
            "important counts, measured latency, scores, findings, risks, and useful suggestions "
            "that are supported by the supplied evidence. Do not output JSON, field names, raw "
            "rows, raw response bodies, or internal execution structures. Do not merely copy the "
            "input results. For local_data runs, calculate requested comparisons or rankings. "
            "Never complain that a target connector is missing."
        ),
        {"goal": goal, "execution": execution},
        **request_options,
    )
    return {"summary": summary}


def assist_job_configuration(
    config: AgentConfig,
    messages: list[dict[str, str]],
    context: dict[str, Any],
) -> dict[str, Any]:
    if not config.enabled or not config.base_url or not config.model:
        return {
            "reply": (
                "请告诉我目标接口地址、返回内容示例、想评测的重点，以及希望导出的格式。"
                "模型规划启用后，我可以把这些信息整理成评测任务。"
            ),
            "draft": {},
        }
    result = request_json(
        config,
        ASSISTANT_PROMPT,
        {"conversation": messages[-12:], "workspace": context},
    )
    return filter_assistant_result(result)
