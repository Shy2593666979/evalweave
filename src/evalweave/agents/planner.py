from __future__ import annotations

import json
from typing import Any

from openai import OpenAI

from evalweave.core.config import AgentConfig

SYSTEM_PROMPT = """You design auditable evaluation plans for companion AI systems.
Return one JSON object only. The plan must normalize the source into cases, evaluate multi-turn
conversation quality, tool calls, latency, and safety, then request blind human review and produce
an evidence-linked summary. Never include credentials or executable code. Use only declarative
operations from this allowlist: normalize, target_call, conversation_eval, tool_eval, latency_eval,
safety_eval, human_review, summarize."""


def request_json(config: AgentConfig, instructions: str, payload: dict[str, Any]) -> dict[str, Any]:
    client = OpenAI(
        api_key=config.api_key or "not-configured",
        base_url=config.base_url,
        timeout=config.timeout_seconds,
        max_retries=0,
    )
    serialized = json.dumps(payload, ensure_ascii=False)
    if config.api_mode == "responses":
        response = client.responses.create(
            model=config.model,
            instructions=instructions,
            input=serialized,
            text={"format": {"type": "json_object"}},
            store=False,
        )
        content = response.output_text
    else:
        response = client.chat.completions.create(
            model=config.model,
            temperature=0,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": instructions},
                {"role": "user", "content": serialized},
            ],
        )
        content = response.choices[0].message.content
    if not content:
        raise ValueError("Agent model returned empty content")
    result = json.loads(content)
    if not isinstance(result, dict):
        raise ValueError("Agent model must return a JSON object")
    return result


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
) -> dict[str, Any]:
    if not config.enabled or not config.base_url or not config.model:
        return build_fallback_spec(goal, discovery)
    spec = request_json(
        config,
        SYSTEM_PROMPT,
        {
            "goal": goal,
            "source_discovery": discovery,
            "input_config": input_config,
            "previous_error": previous_error,
        },
    )
    operations = spec.get("operations", [])
    allowed = {
        "normalize",
        "target_call",
        "conversation_eval",
        "tool_eval",
        "latency_eval",
        "safety_eval",
        "human_review",
        "summarize",
    }
    if not isinstance(operations, list) or set(operations) - allowed:
        raise ValueError("Agent model returned unsupported operations")
    spec["discovery"] = discovery
    return spec


def generate_summary(
    config: AgentConfig, goal: str, execution: dict[str, Any]
) -> dict[str, Any]:
    fallback = {
        "summary": "EvalSpec 已验证，已执行允许的内置连接器。",
        "findings": [execution],
        "recommendations": [],
    }
    if not config.enabled or not config.base_url or not config.model:
        return fallback
    return request_json(
        config,
        (
            "Summarize an evaluation run as JSON with keys summary, findings, risks, and "
            "recommendations. Use only supplied evidence and do not invent causes or scores."
        ),
        {"goal": goal, "execution": execution},
    )
