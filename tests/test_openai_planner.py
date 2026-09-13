from types import SimpleNamespace

import pytest

from evalweave.agents.planner import (
    ASSISTANT_PROMPT,
    derive_task_title,
    extract_partial_json_string,
    generate_eval_spec,
    generate_test_cases,
    request_json,
)
from evalweave.agents.react_runtime import REACT_SYSTEM_PROMPT
from evalweave.core.config import AgentConfig


@pytest.mark.parametrize("api_mode", ["responses", "chat_completions"])
def test_request_json_uses_openai_sdk(monkeypatch, api_mode: str) -> None:
    calls: list[tuple[str, dict]] = []

    class Responses:
        def create(self, **kwargs):
            calls.append(("responses", kwargs))
            return SimpleNamespace(output_text='{"status":"ok"}')

    class Completions:
        def create(self, **kwargs):
            calls.append(("chat_completions", kwargs))
            message = SimpleNamespace(content='{"status":"ok"}')
            return SimpleNamespace(choices=[SimpleNamespace(message=message)])

    class FakeOpenAI:
        def __init__(self, **kwargs):
            calls.append(("client", kwargs))
            self.responses = Responses()
            self.chat = SimpleNamespace(completions=Completions())

    monkeypatch.setattr("evalweave.agents.planner.OpenAI", FakeOpenAI)
    config = AgentConfig(
        enabled=True,
        api_mode=api_mode,
        base_url="https://model.example/v1",
        api_key="secret",
        model="planner-model",
    )

    result = request_json(config, "Return JSON", {"task": "plan"})

    assert result == {"status": "ok"}
    assert calls[0][0] == "client"
    assert calls[0][1]["base_url"] == "https://model.example/v1"
    assert calls[1][0] == api_mode


@pytest.mark.parametrize("api_mode", ["responses", "chat_completions"])
def test_request_json_streams_model_output(monkeypatch, api_mode: str) -> None:
    expected_deltas = ['{"status":', '"ok"}']

    class Responses:
        def create(self, **kwargs):
            assert kwargs["stream"] is True
            return [
                SimpleNamespace(type="response.output_text.delta", delta=delta)
                for delta in expected_deltas
            ]

    class Completions:
        def create(self, **kwargs):
            assert kwargs["stream"] is True
            return [
                SimpleNamespace(
                    choices=[SimpleNamespace(delta=SimpleNamespace(content=delta))]
                )
                for delta in expected_deltas
            ]

    class FakeOpenAI:
        def __init__(self, **_kwargs):
            self.responses = Responses()
            self.chat = SimpleNamespace(completions=Completions())

    monkeypatch.setattr("evalweave.agents.planner.OpenAI", FakeOpenAI)
    config = AgentConfig(
        enabled=True,
        api_mode=api_mode,
        base_url="https://model.example/v1",
        api_key="secret",
        model="planner-model",
    )
    received: list[str] = []

    result = request_json(
        config,
        "Return JSON",
        {"task": "plan"},
        on_delta=received.append,
    )

    assert result == {"status": "ok"}
    assert received == expected_deltas


def test_extract_partial_reply_from_streamed_json() -> None:
    document = '{"reply":"正在分析\\n第'

    assert extract_partial_json_string(document, "reply") == "正在分析\n第"


def test_assistant_owns_title_and_probes_target_response() -> None:
    assert "任务名由你生成" in REACT_SYSTEM_PROMPT
    assert "selected by UI controls" in ASSISTANT_PROMPT
    assert "probe_http_target" in REACT_SYSTEM_PROMPT
    assert derive_task_title("帮我评测客服对话接口的响应质量。") == "评测客服对话接口的响应质量"


def test_local_spreadsheet_plan_normalizes_model_operation_names(monkeypatch) -> None:
    config = AgentConfig(enabled=True, base_url="https://model.test", model="test-model")
    monkeypatch.setattr(
        "evalweave.agents.planner.request_json",
        lambda *_: {
            "plan": [
                {"operation": "parse_spreadsheet", "header_row": 2},
                {"operation": "compare_scores"},
            ],
            "source": {"format": "xlsx"},
        },
    )

    spec = generate_eval_spec(
        config,
        "看下成绩比拼",
        {"format": "xlsx", "fields": []},
        {},
    )

    assert spec["operations"] == ["normalize", "data_analysis", "ranking", "summarize"]


def test_generate_test_cases_requires_exact_object_count(monkeypatch) -> None:
    config = AgentConfig(enabled=True, base_url="https://model.test", model="test-model")
    monkeypatch.setattr(
        "evalweave.agents.planner.request_json",
        lambda *_: {
            "cases": [
                {"input": {"message": "hello"}, "expected": {}, "metadata": {}},
                {"input": {"message": "edge case"}, "expected": {}, "metadata": {}},
            ]
        },
    )

    cases = generate_test_cases(
        config,
        "Evaluate chat quality",
        {"target": {"body": {"message": "{{message}}"}}},
        {"operations": ["target_call"]},
        2,
    )

    assert [case["input"] for case in cases] == [
        {"message": "hello"},
        {"message": "edge case"},
    ]
