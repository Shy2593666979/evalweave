from pathlib import Path
from types import SimpleNamespace

import pytest

from evalweave.agents.react_tools import AssistantToolContext, SendWeComMessageTool
from evalweave.notifications.service import send_wecom, send_wecom_file


class FakeResponse:
    def __init__(self, body: dict[str, object]) -> None:
        self.body = body

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, object]:
        return self.body


def _settings(tmp_path: Path | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        notifications=SimpleNamespace(
            wecom=SimpleNamespace(
                enabled=True,
                webhook_url=(
                    "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=test-key"
                ),
                timeout_seconds=10,
            )
        ),
        storage=SimpleNamespace(local_directory=tmp_path),
    )


@pytest.mark.parametrize("message_type", ["text", "markdown"])
def test_send_wecom_text_messages(monkeypatch, message_type: str) -> None:
    requests: list[dict[str, object]] = []

    def fake_post(url: str, **kwargs):
        requests.append({"url": url, **kwargs})
        return FakeResponse({"errcode": 0, "errmsg": "ok", "msgid": "message-1"})

    monkeypatch.setattr(
        "evalweave.notifications.service.get_settings", lambda: _settings()
    )
    monkeypatch.setattr("evalweave.notifications.service.httpx.post", fake_post)

    result = send_wecom("测试内容", "zhangsan", message_type=message_type)

    assert result == "message-1"
    assert requests[0]["json"]["msgtype"] == message_type
    if message_type == "text":
        assert requests[0]["json"]["text"]["mentioned_list"] == ["zhangsan"]
    else:
        assert requests[0]["json"]["markdown"]["content"].endswith("<@zhangsan>")


def test_send_wecom_file_uploads_then_sends(monkeypatch, tmp_path: Path) -> None:
    source = tmp_path / "stored-file"
    source.write_bytes(b"report")
    requests: list[dict[str, object]] = []

    def fake_post(url: str, **kwargs):
        requests.append({"url": url, **kwargs})
        if "upload_media" in url:
            return FakeResponse({"errcode": 0, "errmsg": "ok", "media_id": "media-1"})
        return FakeResponse({"errcode": 0, "errmsg": "ok"})

    monkeypatch.setattr(
        "evalweave.notifications.service.get_settings", lambda: _settings(tmp_path)
    )
    monkeypatch.setattr("evalweave.notifications.service.httpx.post", fake_post)

    result = send_wecom_file(source, "评测结果.xlsx")

    assert result == "media-1"
    assert requests[0]["url"].endswith("/cgi-bin/webhook/upload_media")
    assert requests[0]["params"] == {"key": "test-key", "type": "file"}
    assert requests[0]["files"]["media"][0] == "评测结果.xlsx"
    assert requests[1]["json"] == {
        "msgtype": "file",
        "file": {"media_id": "media-1"},
    }


def test_send_wecom_rejects_provider_error(monkeypatch) -> None:
    monkeypatch.setattr(
        "evalweave.notifications.service.get_settings", lambda: _settings()
    )
    monkeypatch.setattr(
        "evalweave.notifications.service.httpx.post",
        lambda *_args, **_kwargs: FakeResponse({"errcode": 93000, "errmsg": "bad key"}),
    )

    with pytest.raises(RuntimeError, match="bad key"):
        send_wecom("测试")


def test_wecom_tool_exposes_only_supported_message_types(monkeypatch) -> None:
    sent: list[tuple[str, str, str]] = []

    def fake_send(content: str, recipient: str, *, message_type: str):
        sent.append((content, recipient, message_type))
        return "message-1"

    monkeypatch.setattr("evalweave.agents.react_tools.send_wecom", fake_send)
    tool = SendWeComMessageTool()
    context = AssistantToolContext(draft={}, project_id=None)

    result = tool.run(
        context,
        message_type="markdown",
        content="**评测完成**",
        recipient="zhangsan",
    )

    assert '"ok": true' in result
    assert sent == [("**评测完成**", "zhangsan", "markdown")]
    assert tool.parameters["properties"]["message_type"]["enum"] == [
        "text",
        "markdown",
        "file",
    ]

