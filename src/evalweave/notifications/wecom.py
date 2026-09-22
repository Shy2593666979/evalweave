from pathlib import Path
from typing import Literal
from urllib.parse import parse_qs, urlparse

import httpx

from evalweave.core.config import get_settings

WECOM_FILE_LIMIT_BYTES = 20 * 1024 * 1024


def _wecom_config() -> tuple[str, int]:
    config = get_settings().notifications.wecom
    if not config.enabled or not config.webhook_url:
        raise RuntimeError("企业微信通知尚未配置")
    return config.webhook_url, config.timeout_seconds


def _check_wecom_response(response: httpx.Response, operation: str) -> dict[str, object]:
    response.raise_for_status()
    body = response.json()
    if body.get("errcode") != 0:
        raise RuntimeError(f"企业微信{operation}失败：{body.get('errmsg', '未知错误')}")
    return body


def _wecom_upload_url(webhook_url: str) -> tuple[str, str]:
    parsed = urlparse(webhook_url)
    key = parse_qs(parsed.query).get("key", [""])[0]
    if parsed.scheme != "https" or not parsed.netloc or not key:
        raise RuntimeError("企业微信机器人 Webhook 地址无效")
    return f"{parsed.scheme}://{parsed.netloc}/cgi-bin/webhook/upload_media", key


def send_wecom(
    content: str,
    recipient: str = "",
    *,
    message_type: Literal["text", "markdown", "markdown_v2"] = "text",
) -> str | None:
    webhook_url, timeout_seconds = _wecom_config()
    if message_type == "text":
        text: dict[str, object] = {"content": content}
        if recipient:
            text["mentioned_list"] = [recipient]
        payload: dict[str, object] = {"msgtype": "text", "text": text}
    elif message_type == "markdown":
        markdown = content
        if recipient:
            markdown = f"{markdown}\n<@{recipient}>"
        payload = {"msgtype": "markdown", "markdown": {"content": markdown}}
    elif message_type == "markdown_v2":
        if recipient:
            raise ValueError("企业微信 markdown_v2 不支持 @成员")
        payload = {"msgtype": "markdown_v2", "markdown_v2": {"content": content}}
    else:
        raise ValueError(f"不支持的企业微信消息类型：{message_type}")
    response = httpx.post(webhook_url, json=payload, timeout=timeout_seconds)
    body = _check_wecom_response(response, "消息发送")
    return str(body.get("msgid")) if body.get("msgid") else None


def send_wecom_file(path: Path, file_name: str | None = None) -> str:
    webhook_url, timeout_seconds = _wecom_config()
    if not path.is_file():
        raise FileNotFoundError(f"待发送文件不存在：{path.name}")
    if path.stat().st_size > WECOM_FILE_LIMIT_BYTES:
        raise ValueError("企业微信机器人文件不能超过 20 MB")
    upload_url, key = _wecom_upload_url(webhook_url)
    with path.open("rb") as source:
        upload_response = httpx.post(
            upload_url,
            params={"key": key, "type": "file"},
            files={"media": (file_name or path.name, source, "application/octet-stream")},
            timeout=timeout_seconds,
        )
    upload = _check_wecom_response(upload_response, "文件上传")
    media_id = str(upload.get("media_id") or "")
    if not media_id:
        raise RuntimeError("企业微信文件上传成功但未返回 media_id")
    send_response = httpx.post(
        webhook_url,
        json={"msgtype": "file", "file": {"media_id": media_id}},
        timeout=timeout_seconds,
    )
    _check_wecom_response(send_response, "文件发送")
    return media_id
