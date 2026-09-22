from __future__ import annotations

from datetime import UTC, datetime

from sqlmodel import Session

from evalweave.core.config import get_settings
from evalweave.db.models import (
    AgentJob,
    DeliveryStatus,
    FileObject,
    HumanTask,
    NotificationDelivery,
    User,
)
from evalweave.notifications.email import send_email
from evalweave.notifications.wecom import send_wecom, send_wecom_file
from evalweave.storage import LocalFileStorage


def notify_agent_job_completed(session: Session, job: AgentJob) -> str | None:
    """Send the default evaluation completion notification to WeCom."""
    settings = get_settings()
    if not settings.notifications.wecom.enabled:
        return None
    creator = session.get(User, job.created_by)
    link = f"{settings.notifications.platform_base_url.rstrip('/')}/evaluations/{job.id}"
    summary = job.result.get("summary")
    summary_text = summary.strip() if isinstance(summary, str) else "评测任务已经执行完成。"
    content = (
        "# EvalWeave 评测完成\n\n"
        f"## {job.title}\n\n"
        f"**发起人：** {creator.username if creator else '未知用户'}\n\n"
        f"{summary_text}\n\n"
        f"[查看完整结果]({link})"
    )
    message_id = send_wecom(content, message_type="markdown_v2")
    if job.result_file_id:
        result_file = session.get(FileObject, job.result_file_id)
        if result_file is not None:
            path = LocalFileStorage(settings.storage.local_directory).path_for(
                result_file.storage_key
            )
            send_wecom_file(path, result_file.original_name)
    return message_id


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
