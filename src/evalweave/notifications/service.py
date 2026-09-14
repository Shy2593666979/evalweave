from __future__ import annotations

import smtplib
import ssl
from datetime import UTC, datetime
from email.message import EmailMessage

import httpx
from sqlmodel import Session

from evalweave.core.config import get_settings
from evalweave.db.models import AgentJob, DeliveryStatus, HumanTask, NotificationDelivery, User


def send_wecom(content: str, recipient: str) -> str | None:
    config = get_settings().notifications.wecom
    if not config.enabled or not config.webhook_url:
        raise RuntimeError("WeCom notifications are not configured")
    text: dict[str, object] = {"content": content}
    if recipient:
        text["mentioned_list"] = [recipient]
    response = httpx.post(
        config.webhook_url,
        json={"msgtype": "text", "text": text},
        timeout=config.timeout_seconds,
    )
    response.raise_for_status()
    body = response.json()
    if body.get("errcode") != 0:
        raise RuntimeError(f"WeCom rejected notification: {body.get('errmsg', 'unknown error')}")
    return str(body.get("msgid")) if body.get("msgid") else None


def send_email(subject: str, content: str, recipient: str) -> str | None:
    config = get_settings().notifications.email
    if not config.enabled or not config.host or not config.from_address:
        raise RuntimeError("Email notifications are not configured")
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = config.from_address
    message["To"] = recipient
    message.set_content(content)
    context = ssl.create_default_context()
    if config.use_ssl:
        with smtplib.SMTP_SSL(
            config.host, config.port, timeout=config.timeout_seconds, context=context
        ) as client:
            if config.username:
                client.login(config.username, config.password)
            client.send_message(message)
    else:
        with smtplib.SMTP(config.host, config.port, timeout=config.timeout_seconds) as client:
            if config.starttls:
                client.starttls(context=context)
            if config.username:
                client.login(config.username, config.password)
            client.send_message(message)
    return message.get("Message-ID")


def notify_agent_job_completed(session: Session, job: AgentJob) -> str | None:
    """Email the task creator after an evaluation has completed."""
    settings = get_settings()
    if not settings.notifications.email.enabled:
        return None
    creator = session.get(User, job.created_by)
    if creator is None or not creator.email:
        return None
    link = f"{settings.notifications.platform_base_url.rstrip('/')}/evaluations/{job.id}"
    summary = job.result.get("summary")
    summary_text = summary.strip() if isinstance(summary, str) else "评测任务已经执行完成。"
    content = (
        f"你好，{creator.username}：\n\n"
        f"你发起的评测任务《{job.title}》已经完成。\n\n"
        f"{summary_text}\n\n"
        f"查看完整结果：{link}\n"
    )
    return send_email(f"[EvalWeave] 评测任务已完成：{job.title}", content, creator.email)


def notify_human_task(session: Session, task: HumanTask) -> list[NotificationDelivery]:
    settings = get_settings()
    link = f"{settings.notifications.platform_base_url.rstrip('/')}/human-tasks/{task.id}"
    content = f"{task.title}\n\n{task.instructions}\n\n处理地址：{link}"
    deliveries = []
    for target in task.notification_targets:
        channel = target.get("channel", "")
        recipient = target.get("recipient", "")
        delivery = NotificationDelivery(
            human_task_id=task.id,
            channel=channel,
            recipient=recipient,
        )
        session.add(delivery)
        session.flush()
        try:
            if channel == "wecom":
                delivery.provider_message_id = send_wecom(content, recipient)
            elif channel == "email":
                delivery.provider_message_id = send_email(task.title, content, recipient)
            else:
                raise ValueError(f"Unsupported notification channel: {channel}")
            delivery.status = DeliveryStatus.SENT
            delivery.sent_at = datetime.now(UTC)
        except Exception as error:
            delivery.status = DeliveryStatus.FAILED
            delivery.error = str(error)[:2000]
        delivery.updated_at = datetime.now(UTC)
        session.add(delivery)
        deliveries.append(delivery)
    session.commit()
    return deliveries
