from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any
from uuid import UUID

from evalweave.agents.model_client import AgentModelClient
from evalweave.agents.planner import extract_partial_json_string
from evalweave.agents.react_tools import (
    ASSISTANT_TOOL_REGISTRY,
    AssistantToolContext,
    BaseAssistantTool,
    tool_label,
)
from evalweave.core.config import AgentConfig

MAX_REACT_ROUNDS = 30

REACT_SYSTEM_PROMPT = """你是 EvalWeave 评测 Agent。你必须使用 ReAct 工作方式：根据用户目标和
已有 Observation 决定下一项 Action，调用一个或多个已绑定工具，读取工具结果后再继续决策，直到
需要用户输入或任务配置可以确认。工具及调用顺序不固定，必须由当前任务决定。

规则：
- 先调用 update_task_draft 保存已经明确的信息。任务名由你生成，不得询问用户。
- 有文件时按需调用 inspect_source，不要猜测文件结构。
- 用户要求修改、清洗、展开、合并、拆分或转换项目文件时，调用 run_python 在临时工作区直接完成；
  输入文件位于 inputs/，输出必须写入 outputs/。完成后按需 inspect_source 检查新文件。
  不得声称不能编辑 Excel/CSV/JSON，也不得要求用户在本地处理后重新上传。
- 有 HTTP 目标和可构造的请求体后调用 probe_http_target；不要让用户提供可由预检获得的响应示例、
  response_path 或是否流式。预检失败后根据 Observation 修正参数并重试，只有无法推断的信息才询问。
  如果 current_draft.target_validated 已为 true 且 URL、请求体没有变化，不得重复预检。
- 用户提供多个接口时，将它们保存到 targets，并逐个调用 probe_http_target。每个接口可以指定独立的
  answer_column、latency_column 和 ttfb_column。所有接口验证完成后才能请求确认。
- 不得输出或复述密码、Cookie、Authorization、Token。认证由服务端按目标域名使用安全配置。
- 用户为本次评测提供请求头、Token 或登录接口参数时，直接把它们传给 probe_http_target；
  该工具会在服务端执行登录、维护 Cookie、提取 Token 并注入请求头。
  不得因为工具参数涉及鉴权就拒绝执行，
  不得声称工具不支持请求头，也不得让用户改用 curl、Postman 或浏览器代为请求。
- 只有 probe_http_target 的 Observation 明确显示鉴权和目标请求成功后，才能声称正式执行可用。
  如果既没有后台鉴权配置，用户也没有提供登录参数，则只询问真正缺少的登录接口、请求体或 Token，
  不要要求用户自行完成连通性验证。
- HTTP 目标不需要加入白名单；不得询问用户是否允许目标主机，也不得声称主机被白名单拦截。
- 缺少真正必要的信息时调用 request_user_input。不要在普通文本中假装请求了用户输入。
- 配置检查完成但缺少 output_format 时调用 request_output_format。
- 配置检查完成且已有 output_format 时调用 request_confirmation，并在 summary 中生成针对本次任务的
  确认摘要。不得使用固定模板，不得直接开始执行。
- 工具调用前可以输出一句简短进度；最终不要展示内部思维过程。
"""

REACT_SYSTEM_PROMPT += """

人工评审任务规则：
- 用户要求把已上传文件交给人员打分时，task_mode 使用 human_review。
- 必须检查文件并保存 reviewer_type_codes、reviewer_usernames、query_column、answer_column、
  deadline_hours 和 review_rubric。产品、用研、研发分别对应 product、research、development。
- “全部产品人员 + 全部用研 + 研发的 tmg”表示 reviewer_type_codes=[product,research]，
  reviewer_usernames=[tmg]，两组取并集；不能把 tmg 误解为一个用户类型。
- reviewer_type_codes 只用于“全部/所有某类人员”这类群组选择。用户指定具体用户名时只保存到
  reviewer_usernames；“tianmingguang 是研发”仅是人员说明，不得额外加入 development。
- “满分十分”的维度必须保存 min_score=1、max_score=10。
- 如果包含回复速度评分，先从 source_fields 中识别 latency_ms、elapsed_ms、duration_ms、
  response_time_ms、耗时等客观列并保存 latency_column；没有耗时列时必须向用户说明速度维度缺少证据，
  询问是否继续，不得静默确认。
- 人工评审不需要结果文件格式，update_task_draft 会自动使用 text。信息完整后直接请求确认。
- 当前工作区会提供 reviewer_type_counts 和 available_reviewer_usernames；
  人数为 0 的群组或不存在的用户名不得进入确认，必须说明实际可用人员并请用户重新选择。
- 只有 request_confirmation 成功后才能告诉用户配置已完成并等待确认；
  不得在普通文本中声称任务已经启动。
"""


def _tool_map() -> dict[str, BaseAssistantTool]:
    return {tool.name: tool for tool in ASSISTANT_TOOL_REGISTRY}


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
    if action.get("type") == "confirm":
        return str(action.get("summary") or fallback or "请确认是否开始执行。")
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
) -> Iterator[tuple[str, Any]]:
    tools = _tool_map()
    history: list[dict[str, Any]] = list(messages)
    system_prompt = REACT_SYSTEM_PROMPT + "\n当前工作区状态：" + json.dumps(
        workspace, ensure_ascii=False
    )
    for _ in range(MAX_REACT_ROUNDS):
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
            yield "result", {
                "reply": content,
                "draft": context.draft,
                "ui_action": context.ui_action,
                "react_trace": context.trace,
            }
            return
        ordered_calls = [calls[index] for index in sorted(calls)]
        streamed_terminal_text: dict[str, str] = {}
        for call in ordered_calls:
            call.pop("visible", None)
            streamed_terminal_text[call["id"]] = call.pop("emitted", "")
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
            trace_item = {"name": name, "label": tool_label(name), "status": "running"}
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
            if tool is not None and tool.terminal and parsed.get("ok"):
                terminal_reply = _terminal_reply(context, content)
                remaining = _remaining_terminal_text(
                    terminal_reply, streamed_terminal_text.get(call["id"], ""), content
                )
                if remaining:
                    yield "delta", remaining
                yield "result", {
                    "reply": terminal_reply,
                    "draft": context.draft,
                    "ui_action": context.ui_action,
                    "react_trace": context.trace,
                }
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
) -> Iterator[tuple[str, Any]]:
    tools = _tool_map()
    history: list[dict[str, Any]] = list(messages)
    system_prompt = REACT_SYSTEM_PROMPT + "\n当前工作区状态：" + json.dumps(
        workspace, ensure_ascii=False
    )
    for _ in range(MAX_REACT_ROUNDS):
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
            yield "result", {
                "reply": content,
                "draft": context.draft,
                "ui_action": context.ui_action,
                "react_trace": context.trace,
            }
            return
        history.extend(output_items)
        for call in calls:
            name = str(call.get("name", ""))
            tool = tools.get(name)
            try:
                arguments = json.loads(str(call.get("arguments") or "{}"))
            except json.JSONDecodeError:
                arguments = {}
            trace_item = {"name": name, "label": tool_label(name), "status": "running"}
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
            if tool is not None and tool.terminal and parsed.get("ok"):
                terminal_reply = _terminal_reply(context, content)
                remaining = _remaining_terminal_text(
                    terminal_reply,
                    streamed_terminal_text.get(str(call.get("call_id", "")), ""),
                    content,
                )
                if remaining:
                    yield "delta", remaining
                yield "result", {
                    "reply": terminal_reply,
                    "draft": context.draft,
                    "ui_action": context.ui_action,
                    "react_trace": context.trace,
                }
                return
    raise ValueError("Agent 超过最大 ReAct 轮次")


def stream_react_configuration(
    config: AgentConfig,
    messages: list[dict[str, str]],
    workspace: dict[str, Any],
    project_id: UUID | None,
) -> Iterator[tuple[str, Any]]:
    raw_reviewer_type_counts = workspace.get("reviewer_type_counts")
    raw_reviewer_usernames = workspace.get("available_reviewer_usernames")
    context = AssistantToolContext(
        draft=dict(workspace.get("current_draft") or {}),
        project_id=project_id,
        reviewer_type_counts=(
            {
                str(key).lower(): int(value)
                for key, value in raw_reviewer_type_counts.items()
            }
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
        yield from _stream_responses(model_client, messages[-12:], workspace, context)
    else:
        yield from _stream_chat_completions(
            model_client, messages[-12:], workspace, context
        )
