from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any
from uuid import UUID

from evalweave.agents.model_client import AgentModelClient
from evalweave.agents.planner import extract_partial_json_string
from evalweave.agents.prompts import RUNTIME_SYSTEM_PROMPT
from evalweave.agents.tools import (
    ASSISTANT_TOOL_REGISTRY,
    AssistantToolContext,
    BaseAssistantTool,
    tool_label,
)
from evalweave.core.config import AgentConfig

MAX_REACT_ROUNDS = 30


def _tool_map(allowed_tool_names: set[str] | None = None) -> dict[str, BaseAssistantTool]:
    return {
        tool.name: tool
        for tool in ASSISTANT_TOOL_REGISTRY
        if (
            tool.name != "repair_python_job_script"
            if allowed_tool_names is None
            else tool.name in allowed_tool_names
        )
    }


def _run_tool(
    tool: BaseAssistantTool,
    arguments: dict[str, Any],
    context: AssistantToolContext,
) -> str:
    try:
        return tool.run(context, **arguments)
    except Exception as error:
        return json.dumps(
            {"ok": False, "error": str(error)[:2000]},
            ensure_ascii=False,
        )


def _terminal_reply(context: AssistantToolContext, fallback: str) -> str:
    action = context.ui_action or {}
    if action.get("type") == "background_job":
        job_id = str(action.get("job_id") or "")
        return f"后台任务已启动，你可以前往[评测任务](/evaluations/{job_id})查看运行状态和结果。"
    if action.get("type") == "confirm":
        return str(action.get("summary") or fallback or "请确认是否开始执行。")
    if action.get("type") == "start_task":
        return str(action.get("summary") or fallback or "配置已完成，正在启动任务。")
    if action.get("type") == "python_repaired":
        return str(action.get("summary") or fallback or "脚本已修复，正在重新执行。")
    if action.get("type") == "user_input":
        return str(action.get("question") or fallback or "请补充必要信息。")
    if action.get("type") == "choose_output":
        return str(action.get("question") or fallback or "请选择结果交付方式。")
    return fallback


_TERMINAL_TEXT_FIELDS = {
    "request_confirmation": "summary",
    "request_user_input": "question",
}


def _terminal_argument_delta(
    tool_name: str,
    arguments: str,
    emitted: str,
) -> tuple[str, str]:
    field = _TERMINAL_TEXT_FIELDS.get(tool_name)
    if field is None:
        return "", emitted
    visible = extract_partial_json_string(arguments, field)
    if not visible.startswith(emitted) or len(visible) <= len(emitted):
        return "", emitted
    return visible[len(emitted) :], visible


def _remaining_terminal_text(full_text: str, emitted: str, preceding: str = "") -> str:
    if full_text.strip() and preceding.rstrip().endswith(full_text.strip()):
        return ""
    if full_text.startswith(emitted):
        return full_text[len(emitted) :]
    return full_text


def _stream_chat_completions(
    model_client: AgentModelClient,
    messages: list[dict[str, Any]],
    workspace: dict[str, Any],
    context: AssistantToolContext,
    allowed_tool_names: set[str] | None = None,
    max_rounds: int = MAX_REACT_ROUNDS,
) -> Iterator[tuple[str, Any]]:
    tools = _tool_map(allowed_tool_names)
    history: list[dict[str, Any]] = list(messages)
    system_prompt = (
        RUNTIME_SYSTEM_PROMPT + "\n当前工作区状态：" + json.dumps(workspace, ensure_ascii=False)
    )
    for _ in range(max_rounds):
        stream = model_client.stream_tools(
            system_prompt,
            history,
            [tool.to_model_def() for tool in tools.values()],
        )
        content = ""
        calls: dict[int, dict[str, Any]] = {}
        for chunk in stream:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            piece = getattr(delta, "content", None)
            if piece:
                content += piece
                yield "delta", piece
            for call_delta in getattr(delta, "tool_calls", None) or []:
                entry = calls.setdefault(
                    call_delta.index,
                    {
                        "id": "",
                        "type": "function",
                        "function": {"name": "", "arguments": ""},
                        "visible": "",
                        "emitted": "",
                    },
                )
                if call_delta.id:
                    entry["id"] = call_delta.id
                function = call_delta.function
                if function and function.name:
                    entry["function"]["name"] += function.name
                if function and function.arguments:
                    entry["function"]["arguments"] += function.arguments
                    visible_delta, visible = _terminal_argument_delta(
                        entry["function"]["name"],
                        entry["function"]["arguments"],
                        entry["visible"],
                    )
                    entry["visible"] = visible
                    tool = tools.get(entry["function"]["name"])
                    if (
                        visible_delta
                        and tool is not None
                        and tool.can_stream_terminal_text(context)
                    ):
                        entry["emitted"] = visible
                        yield "delta", visible_delta
        if not calls:
            yield (
                "result",
                {
                    "reply": content,
                    "draft": context.draft,
                    "ui_action": context.ui_action,
                    "react_trace": context.trace,
                },
            )
            return
        ordered_calls = [calls[index] for index in sorted(calls)]
        streamed_terminal_text: dict[str, str] = {}
        for call in ordered_calls:
            call.pop("visible", None)
            streamed_terminal_text[call["id"]] = call.pop("emitted", "")
        if not any(streamed_terminal_text.values()):
            yield "round_end", None
        history.append(
            {"role": "assistant", "content": content or None, "tool_calls": ordered_calls}
        )
        for call in ordered_calls:
            name = call["function"]["name"]
            tool = tools.get(name)
            try:
                arguments = json.loads(call["function"]["arguments"] or "{}")
            except json.JSONDecodeError:
                arguments = {}
            step_title = str(arguments.get("step_title") or "").strip()
            trace_item = {
                "name": name,
                "label": step_title[:128] or tool_label(name),
                "status": "running",
            }
            context.trace.append(trace_item)
            yield "tool_start", trace_item
            if tool is None:
                result = json.dumps({"ok": False, "error": f"未知工具：{name}"}, ensure_ascii=False)
            else:
                result = _run_tool(tool, arguments, context)
            parsed = json.loads(result)
            trace_item["status"] = "completed" if parsed.get("ok") else "failed"
            trace_item["summary"] = parsed
            yield "tool_result", trace_item
            history.append({"role": "tool", "tool_call_id": call["id"], "content": result})
            if tool is not None and (tool.terminal or parsed.get("queued")) and parsed.get("ok"):
                terminal_reply = _terminal_reply(context, content)
                remaining = _remaining_terminal_text(
                    terminal_reply, streamed_terminal_text.get(call["id"], ""), content
                )
                if remaining:
                    yield "delta", remaining
                yield (
                    "result",
                    {
                        "reply": terminal_reply,
                        "draft": context.draft,
                        "ui_action": context.ui_action,
                        "react_trace": context.trace,
                    },
                )
                return
    raise ValueError("Agent 超过最大 ReAct 轮次")


def _dump_item(item: Any) -> dict[str, Any]:
    if hasattr(item, "model_dump"):
        return item.model_dump(exclude_none=True)
    if isinstance(item, dict):
        return item
    raise ValueError("无法解析模型工具调用")


def _stream_responses(
    model_client: AgentModelClient,
    messages: list[dict[str, Any]],
    workspace: dict[str, Any],
    context: AssistantToolContext,
    allowed_tool_names: set[str] | None = None,
    max_rounds: int = MAX_REACT_ROUNDS,
) -> Iterator[tuple[str, Any]]:
    tools = _tool_map(allowed_tool_names)
    history: list[dict[str, Any]] = list(messages)
    system_prompt = (
        RUNTIME_SYSTEM_PROMPT + "\n当前工作区状态：" + json.dumps(workspace, ensure_ascii=False)
    )
    for _ in range(max_rounds):
        stream = model_client.stream_tools(
            system_prompt,
            history,
            [tool.to_model_def() for tool in tools.values()],
        )
        content = ""
        output_items: list[dict[str, Any]] = []
        call_states: dict[str, dict[str, str]] = {}
        streamed_terminal_text: dict[str, str] = {}
        for event in stream:
            event_type = getattr(event, "type", "")
            if event_type == "response.output_text.delta" and getattr(event, "delta", None):
                piece = str(event.delta)
                content += piece
                yield "delta", piece
            elif event_type == "response.output_item.added" and getattr(event, "item", None):
                item = _dump_item(event.item)
                if item.get("type") == "function_call":
                    state = {
                        "name": str(item.get("name", "")),
                        "arguments": str(item.get("arguments", "")),
                        "visible": "",
                        "emitted": "",
                    }
                    output_index = str(getattr(event, "output_index", ""))
                    item_id = str(item.get("id", ""))
                    if output_index:
                        call_states[output_index] = state
                    if item_id:
                        call_states[item_id] = state
            elif event_type == "response.function_call_arguments.delta" and getattr(
                event, "delta", None
            ):
                output_index = str(getattr(event, "output_index", ""))
                item_id = str(getattr(event, "item_id", ""))
                state = call_states.get(output_index) or call_states.get(item_id)
                if state is not None:
                    state["arguments"] += str(event.delta)
                    visible_delta, visible = _terminal_argument_delta(
                        state["name"], state["arguments"], state["visible"]
                    )
                    state["visible"] = visible
                    tool = tools.get(state["name"])
                    if (
                        visible_delta
                        and tool is not None
                        and tool.can_stream_terminal_text(context)
                    ):
                        state["emitted"] = visible
                        yield "delta", visible_delta
            elif event_type == "response.output_item.done" and getattr(event, "item", None):
                item = _dump_item(event.item)
                output_index = str(getattr(event, "output_index", ""))
                item_id = str(item.get("id", ""))
                state = call_states.get(output_index) or call_states.get(item_id)
                if state is not None:
                    streamed_terminal_text[str(item.get("call_id", ""))] = state["emitted"]
                output_items.append(item)
        calls = [item for item in output_items if item.get("type") == "function_call"]
        if not calls:
            yield (
                "result",
                {
                    "reply": content,
                    "draft": context.draft,
                    "ui_action": context.ui_action,
                    "react_trace": context.trace,
                },
            )
            return
        if not any(streamed_terminal_text.values()):
            yield "round_end", None
        history.extend(output_items)
        for call in calls:
            name = str(call.get("name", ""))
            tool = tools.get(name)
            try:
                arguments = json.loads(str(call.get("arguments") or "{}"))
            except json.JSONDecodeError:
                arguments = {}
            step_title = str(arguments.get("step_title") or "").strip()
            trace_item = {
                "name": name,
                "label": step_title[:128] or tool_label(name),
                "status": "running",
            }
            context.trace.append(trace_item)
            yield "tool_start", trace_item
            if tool is None:
                result = json.dumps({"ok": False, "error": f"未知工具：{name}"}, ensure_ascii=False)
            else:
                result = _run_tool(tool, arguments, context)
            parsed = json.loads(result)
            trace_item["status"] = "completed" if parsed.get("ok") else "failed"
            trace_item["summary"] = parsed
            yield "tool_result", trace_item
            history.append(
                {
                    "type": "function_call_output",
                    "call_id": call.get("call_id"),
                    "output": result,
                }
            )
            if tool is not None and (tool.terminal or parsed.get("queued")) and parsed.get("ok"):
                terminal_reply = _terminal_reply(context, content)
                remaining = _remaining_terminal_text(
                    terminal_reply,
                    streamed_terminal_text.get(str(call.get("call_id", "")), ""),
                    content,
                )
                if remaining:
                    yield "delta", remaining
                yield (
                    "result",
                    {
                        "reply": terminal_reply,
                        "draft": context.draft,
                        "ui_action": context.ui_action,
                        "react_trace": context.trace,
                    },
                )
                return
    raise ValueError("Agent 超过最大 ReAct 轮次")


def stream_react_configuration(
    config: AgentConfig,
    messages: list[dict[str, str]],
    workspace: dict[str, Any],
    project_id: UUID | None,
    actor_id: UUID | None = None,
    conversation_id: UUID | None = None,
    repair_job_id: UUID | None = None,
    allowed_tool_names: set[str] | None = None,
    max_rounds: int = MAX_REACT_ROUNDS,
) -> Iterator[tuple[str, Any]]:
    raw_reviewer_type_counts = workspace.get("reviewer_type_counts")
    raw_reviewer_usernames = workspace.get("available_reviewer_usernames")
    context = AssistantToolContext(
        draft=dict(workspace.get("current_draft") or {}),
        project_id=project_id,
        actor_id=actor_id,
        conversation_id=conversation_id,
        repair_job_id=repair_job_id,
        reviewer_type_counts=(
            {str(key).lower(): int(value) for key, value in raw_reviewer_type_counts.items()}
            if isinstance(raw_reviewer_type_counts, dict)
            else None
        ),
        available_reviewer_usernames=(
            {str(item).lower() for item in raw_reviewer_usernames}
            if isinstance(raw_reviewer_usernames, list)
            else None
        ),
    )
    model_client = AgentModelClient(config)
    if config.api_mode == "responses":
        yield from _stream_responses(
            model_client,
            messages[-12:],
            workspace,
            context,
            allowed_tool_names,
            max_rounds,
        )
    else:
        yield from _stream_chat_completions(
            model_client,
            messages[-12:],
            workspace,
            context,
            allowed_tool_names,
            max_rounds,
        )
