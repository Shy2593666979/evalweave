from types import SimpleNamespace

import pytest

from evalweave.agents.planner import request_json
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
