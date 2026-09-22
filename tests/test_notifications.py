import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from evalweave.agents.tools import AssistantToolContext, SendWeComMessageTool
from evalweave.notifications.email import send_email
from evalweave.notifications.service import notify_agent_job_completed
from evalweave.notifications.wecom import send_wecom, send_wecom_file


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
                webhook_url=("https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=test-key"),
                timeout_seconds=10,
            )
        ),
        storage=SimpleNamespace(local_directory=tmp_path),
    )


def test_send_email_uses_configured_smtp_channel(monkeypatch) -> None:
    sent = []

    class FakeSmtp:
        def __init__(self, host, port, *, timeout):
            assert (host, port, timeout) == ("smtp.example.com", 587, 10)

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def starttls(self, *, context):
            assert context is not None

        def login(self, username, password):
            assert (username, password) == ("sender", "secret")

        def send_message(self, message):
            sent.append(message)

    settings = SimpleNamespace(
        notifications=SimpleNamespace(
            email=SimpleNamespace(
                enabled=True,
                host="smtp.example.com",
                port=587,
                from_address="sender@example.com",
                timeout_seconds=10,
                use_ssl=False,
                starttls=True,
                username="sender",
                password="secret",
            )
        )
    )
    monkeypatch.setattr("evalweave.notifications.email.get_settings", lambda: settings)
    monkeypatch.setattr("evalweave.notifications.email.smtplib.SMTP", FakeSmtp)

    assert send_email("评测完成", "结果已经生成", "user@example.com") is None
    assert sent[0]["Subject"] == "评测完成"
    assert sent[0]["To"] == "user@example.com"


@pytest.mark.parametrize("message_type", ["text", "markdown", "markdown_v2"])
def test_send_wecom_text_messages(monkeypatch, message_type: str) -> None:
    requests: list[dict[str, object]] = []

    def fake_post(url: str, **kwargs):
        requests.append({"url": url, **kwargs})
        return FakeResponse({"errcode": 0, "errmsg": "ok", "msgid": "message-1"})

    monkeypatch.setattr("evalweave.notifications.wecom.get_settings", lambda: _settings())
    monkeypatch.setattr("evalweave.notifications.wecom.httpx.post", fake_post)

    recipient = "" if message_type == "markdown_v2" else "zhangsan"
    result = send_wecom("测试内容", recipient, message_type=message_type)

    assert result == "message-1"
    assert requests[0]["json"]["msgtype"] == message_type
    if message_type == "text":
        assert requests[0]["json"]["text"]["mentioned_list"] == ["zhangsan"]
    elif message_type == "markdown":
        assert requests[0]["json"]["markdown"]["content"].endswith("<@zhangsan>")
    else:
        assert requests[0]["json"] == {
            "msgtype": "markdown_v2",
            "markdown_v2": {"content": "测试内容"},
        }


def test_send_wecom_markdown_v2_rejects_recipient(monkeypatch) -> None:
    monkeypatch.setattr("evalweave.notifications.wecom.get_settings", lambda: _settings())

    with pytest.raises(ValueError, match="markdown_v2 不支持 @成员"):
        send_wecom("测试内容", "zhangsan", message_type="markdown_v2")


def test_send_wecom_file_uploads_then_sends(monkeypatch, tmp_path: Path) -> None:
    source = tmp_path / "stored-file"
    source.write_bytes(b"report")
    requests: list[dict[str, object]] = []

    def fake_post(url: str, **kwargs):
        requests.append({"url": url, **kwargs})
        if "upload_media" in url:
            return FakeResponse({"errcode": 0, "errmsg": "ok", "media_id": "media-1"})
        return FakeResponse({"errcode": 0, "errmsg": "ok"})

    monkeypatch.setattr("evalweave.notifications.wecom.get_settings", lambda: _settings(tmp_path))
    monkeypatch.setattr("evalweave.notifications.wecom.httpx.post", fake_post)

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
    monkeypatch.setattr("evalweave.notifications.wecom.get_settings", lambda: _settings())
    monkeypatch.setattr(
        "evalweave.notifications.wecom.httpx.post",
        lambda *_args, **_kwargs: FakeResponse({"errcode": 93000, "errmsg": "bad key"}),
    )

    with pytest.raises(RuntimeError, match="bad key"):
        send_wecom("测试")


def test_wecom_tool_exposes_only_supported_message_types(monkeypatch) -> None:
    sent: list[tuple[str, str, str]] = []

    def fake_send(content: str, recipient: str, *, message_type: str):
        sent.append((content, recipient, message_type))
        return "message-1"

    monkeypatch.setattr("evalweave.agents.tools.send_wecom", fake_send)
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
        "markdown_v2",
        "file",
    ]


def test_wecom_file_tool_sends_notice_before_file(monkeypatch, tmp_path: Path) -> None:
    project_id = uuid4()
    file_id = uuid4()
    source = tmp_path / "stored-file"
    source.write_bytes(b"report")
    file_object = SimpleNamespace(
        id=file_id,
        project_id=project_id,
        original_name="dev环境评测结果.xlsx",
        storage_key="stored-file",
    )
    sent: list[tuple[str, str]] = []

    class FakeSession:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def get(self, _model, _file_id):
            return file_object

    class FakeStorage:
        def __init__(self, _directory):
            pass

        def path_for(self, _storage_key):
            return source

    def fake_send(content: str, recipient: str, *, message_type: str):
        sent.append((message_type, content))
        assert recipient == "zhangsan"
        return "notice-1"

    def fake_send_file(path: Path, file_name: str):
        sent.append(("file", file_name))
        assert path == source
        return "media-1"

    monkeypatch.setattr("evalweave.agents.tools.Session", lambda _engine: FakeSession())
    monkeypatch.setattr("evalweave.agents.tools.get_settings_engine", lambda: object())
    monkeypatch.setattr(
        "evalweave.agents.tools.get_settings",
        lambda: SimpleNamespace(storage=SimpleNamespace(local_directory=tmp_path)),
    )
    monkeypatch.setattr("evalweave.agents.tools.LocalFileStorage", FakeStorage)
    monkeypatch.setattr("evalweave.agents.tools.send_wecom", fake_send)
    monkeypatch.setattr("evalweave.agents.tools.send_wecom_file", fake_send_file)

    result = json.loads(
        SendWeComMessageTool().run(
            AssistantToolContext(draft={}, project_id=project_id),
            message_type="file",
            source_file_id=str(file_id),
            recipient="zhangsan",
        )
    )

    assert sent == [
        ("text", "dev环境评测结果.xlsx 已生成，请查收。"),
        ("file", "dev环境评测结果.xlsx"),
    ]
    assert result["notice_message_id"] == "notice-1"
    assert result["provider_message_id"] == "media-1"


def test_completed_evaluation_defaults_to_wecom_and_sends_result_file(
    monkeypatch, tmp_path: Path
) -> None:
    job_id = uuid4()
    creator_id = uuid4()
    file_id = uuid4()
    stored = tmp_path / "stored-result"
    stored.write_bytes(b"xlsx")
    creator = SimpleNamespace(username="developer")
    result_file = SimpleNamespace(
        storage_key="stored-result",
        original_name="评测结果.xlsx",
    )
    job = SimpleNamespace(
        id=job_id,
        created_by=creator_id,
        title="日常问答评测",
        result={"summary": "50 条数据已完成。"},
        result_file_id=file_id,
    )
    sent: list[tuple[str, str]] = []

    class FakeSession:
        def get(self, model, object_id):
            if model.__name__ == "User" and object_id == creator_id:
                return creator
            if model.__name__ == "FileObject" and object_id == file_id:
                return result_file
            return None

    monkeypatch.setattr(
        "evalweave.notifications.service.get_settings",
        lambda: SimpleNamespace(
            notifications=SimpleNamespace(
                platform_base_url="http://localhost:5173",
                wecom=SimpleNamespace(enabled=True),
            ),
            storage=SimpleNamespace(local_directory=tmp_path),
        ),
    )
    monkeypatch.setattr(
        "evalweave.notifications.service.send_wecom",
        lambda content, *, message_type: (
            sent.append((message_type, content)) or "message-1"
        ),
    )
    monkeypatch.setattr(
        "evalweave.notifications.service.send_wecom_file",
        lambda path, file_name: sent.append(("file", f"{path.name}:{file_name}"))
        or "media-1",
    )

    message_id = notify_agent_job_completed(FakeSession(), job)

    assert message_id == "message-1"
    assert sent[0][0] == "markdown_v2"
    assert "日常问答评测" in sent[0][1]
    assert f"/evaluations/{job_id}" in sent[0][1]
    assert sent[1] == ("file", "stored-result:评测结果.xlsx")
