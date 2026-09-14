from types import SimpleNamespace

from evalweave.agents.react_runtime import stream_react_configuration
from evalweave.agents.react_tools import (
    AssistantToolContext,
    RequestConfirmationTool,
    RequestUserInputTool,
    UpdateTaskDraftTool,
)
from evalweave.api.routes.agents import redact_sensitive_content
from evalweave.core.config import AgentConfig


def _tool_chunk(index: int, call_id: str, name: str, arguments: str):
    function = SimpleNamespace(name=name, arguments=arguments)
    call = SimpleNamespace(index=index, id=call_id, function=function)
    delta = SimpleNamespace(content=None, tool_calls=[call])
    return SimpleNamespace(choices=[SimpleNamespace(delta=delta)])


def test_chat_completion_react_loop_dispatches_tools(monkeypatch) -> None:
    turns = iter(
        [
            [
                _tool_chunk(
                    0,
                    "call-draft",
                    "update_task_draft",
                    '{"title":"接口评测","goal":"验证接口质量","task_mode":"generated_target"}',
                )
            ],
            [
                _tool_chunk(
                    0,
                    "call-input",
                    "request_user_input",
                    '{"question":"请提供目标接口地址。","options":[]}',
                )
            ],
        ]
    )

    class Completions:
        def create(self, **kwargs):
            assert all(
                tool.get("type") == "function" and "function" in tool
                for tool in kwargs["tools"]
            )
            return next(turns)

    class FakeOpenAI:
        def __init__(self, **_):
            self.chat = SimpleNamespace(completions=Completions())

    monkeypatch.setattr("evalweave.agents.model_client.OpenAI", FakeOpenAI)
    config = AgentConfig(
        enabled=True,
        api_mode="chat_completions",
        base_url="https://model.test/v1",
        model="test-model",
    )

    events = list(
        stream_react_configuration(
            config,
            [{"role": "user", "content": "帮我评测接口"}],
            {"current_draft": {}},
            None,
        )
    )

    result = events[-1][1]
    assert [event[0] for event in events] == [
        "tool_start",
        "tool_result",
        "delta",
        "tool_start",
        "tool_result",
        "result",
    ]
    assert result["draft"]["title"] == "接口评测"
    assert result["ui_action"]["type"] == "user_input"
    assert result["reply"] == "请提供目标接口地址。"


def test_responses_react_streams_terminal_tool_text(monkeypatch) -> None:
    added_item = {
        "id": "item-confirm",
        "type": "function_call",
        "call_id": "call-confirm",
        "name": "request_confirmation",
        "arguments": "",
    }
    completed_item = {
        **added_item,
        "arguments": '{"summary":"确认并启动本次评测任务。"}',
    }
    stream = [
        SimpleNamespace(type="response.output_item.added", output_index=0, item=added_item),
        SimpleNamespace(
            type="response.function_call_arguments.delta",
            output_index=0,
            item_id="item-confirm",
            delta='{"summary":"确认并',
        ),
        SimpleNamespace(
            type="response.function_call_arguments.delta",
            output_index=0,
            item_id="item-confirm",
            delta='启动本次评测任务。"}',
        ),
        SimpleNamespace(type="response.output_item.done", output_index=0, item=completed_item),
    ]

    class Responses:
        def create(self, **kwargs):
            assert all(
                tool.get("type") == "function"
                and "name" in tool
                and "function" not in tool
                for tool in kwargs["tools"]
            )
            return stream

    class FakeOpenAI:
        def __init__(self, **_):
            self.responses = Responses()

    monkeypatch.setattr("evalweave.agents.model_client.OpenAI", FakeOpenAI)
    config = AgentConfig(
        enabled=True,
        api_mode="responses",
        base_url="https://model.test/v1",
        model="test-model",
    )

    events = list(
        stream_react_configuration(
            config,
            [{"role": "user", "content": "开始评测"}],
            {
                "current_draft": {
                    "title": "接口评测",
                    "goal": "评测接口质量",
                    "target_url": "https://example.test/chat",
                    "target_body": {"query": "hello"},
                    "target_validated": True,
                    "output_format": "xlsx",
                }
            },
            None,
        )
    )

    deltas = [value for kind, value in events if kind == "delta"]
    assert deltas == ["确认并", "启动本次评测任务。"]
    assert "".join(deltas) == "确认并启动本次评测任务。"
    assert events[-1][0] == "result"
    assert events[-1][1]["reply"] == "确认并启动本次评测任务。"


def test_failed_confirmation_text_is_not_streamed(monkeypatch) -> None:
    failed_confirmation = {
        "id": "item-confirm",
        "type": "function_call",
        "call_id": "call-confirm",
        "name": "request_confirmation",
        "arguments": '{"summary":"任务已经启动。"}',
    }
    request_input = {
        "id": "item-input",
        "type": "function_call",
        "call_id": "call-input",
        "name": "request_user_input",
        "arguments": '{"question":"请补充评测文件。"}',
    }
    turns = iter(
        [
            [
                SimpleNamespace(
                    type="response.output_item.added",
                    output_index=0,
                    item=failed_confirmation,
                ),
                SimpleNamespace(
                    type="response.function_call_arguments.delta",
                    output_index=0,
                    item_id="item-confirm",
                    delta='{"summary":"任务已经启动。"}',
                ),
                SimpleNamespace(
                    type="response.output_item.done",
                    output_index=0,
                    item=failed_confirmation,
                ),
            ],
            [
                SimpleNamespace(
                    type="response.output_item.added",
                    output_index=0,
                    item=request_input,
                ),
                SimpleNamespace(
                    type="response.output_item.done",
                    output_index=0,
                    item=request_input,
                ),
            ],
        ]
    )

    class Responses:
        def create(self, **_):
            return next(turns)

    class FakeOpenAI:
        def __init__(self, **_):
            self.responses = Responses()

    monkeypatch.setattr("evalweave.agents.model_client.OpenAI", FakeOpenAI)
    config = AgentConfig(
        enabled=True,
        api_mode="responses",
        base_url="https://model.test/v1",
        model="test-model",
    )

    events = list(
        stream_react_configuration(
            config,
            [{"role": "user", "content": "确认"}],
            {"current_draft": {}},
            None,
        )
    )

    streamed = "".join(value for kind, value in events if kind == "delta")
    assert streamed == "请补充评测文件。"
    assert "任务已经启动" not in streamed


def test_human_review_confirmation_derives_goal_when_model_omits_it() -> None:
    context = AssistantToolContext(draft={}, project_id=None)
    update = UpdateTaskDraftTool()

    result = update.run(
        context,
        task_mode="human_review",
        title="人工三维度评测",
        source_file_id="source-id",
        reviewer_usernames=["tianmingguang"],
        deadline_hours=3,
        review_rubric=[
            {"key": "speed", "label": "速度", "min_score": 1, "max_score": 10},
            {"key": "accuracy", "label": "准确率", "min_score": 1, "max_score": 10},
            {"key": "effect", "label": "效果", "min_score": 1, "max_score": 10},
        ],
    )
    context.draft["source_inspected"] = True

    assert '"ok": true' in result
    assert context.draft["output_format"] == "text"
    assert "速度、准确率、效果" in context.draft["goal"]

    confirmation = RequestConfirmationTool().run(context, summary="请确认并开启任务。")

    assert '"ok": true' in confirmation
    assert context.ui_action == {"type": "confirm", "summary": "请确认并开启任务。"}


def test_human_review_confirmation_rejects_empty_reviewer_group() -> None:
    context = AssistantToolContext(
        draft={
            "task_mode": "human_review",
            "title": "人工评审",
            "goal": "评审回复质量",
            "source_file_id": "source-id",
            "source_inspected": True,
            "reviewer_type_codes": ["product"],
            "deadline_hours": 1,
            "review_rubric": [
                {"key": "quality", "label": "效果", "min_score": 1, "max_score": 10}
            ],
            "output_format": "text",
        },
        project_id=None,
        reviewer_type_counts={"product": 0, "development": 1},
        available_reviewer_usernames={"tianmingguang"},
    )

    confirmation = RequestConfirmationTool().run(context, summary="请确认执行。")

    assert '"ok": false' in confirmation
    assert "product" in confirmation
    assert context.ui_action is None


def test_output_format_user_input_is_normalized_to_output_chooser() -> None:
    context = AssistantToolContext(draft={}, project_id=None)

    result = RequestUserInputTool().run(
        context,
        question="请选择本次评测结果的交付格式？",
        options=["xlsx", "jsonl", "markdown", "text"],
    )

    assert '"waiting_for": "output_format"' in result
    assert context.ui_action == {
        "type": "choose_output",
        "question": "请选择本次评测结果的交付格式？",
    }


def test_sensitive_curl_values_are_redacted_before_persistence() -> None:
    content = (
        "-H 'authorization: Bearer abc.def' "
        "-b 'access_token_cookie=secret; refresh_token_cookie=secret2' "
        '--data-raw \'{"user_name":"demo","user_password":"password123"}\''
    )

    redacted = redact_sensitive_content(content)

    assert "abc.def" not in redacted
    assert "access_token_cookie=secret" not in redacted
    assert "password123" not in redacted
    assert redacted.count("[REDACTED]") == 3
