from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlmodel import Session, select

from evalweave.agents.model_config import resolve_agent_config
from evalweave.agents.planner import request_text
from evalweave.db.models import (
    AgentJob,
    AgentJobStatus,
    AssistantConversation,
    AssistantMessage,
    HumanReviewAssignment,
    HumanReviewCampaign,
    HumanReviewItem,
)
from evalweave.db.session import get_engine


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def campaign_is_due(campaign: HumanReviewCampaign, now: datetime | None = None) -> bool:
    deadline = _as_utc(campaign.deadline_at)
    return bool(deadline and deadline <= (now or datetime.now(UTC)))


def campaign_is_ready(
    campaign: HumanReviewCampaign,
    assignments: list[HumanReviewAssignment],
    now: datetime | None = None,
) -> tuple[bool, str | None]:
    completed = sum(item.status == "submitted" for item in assignments)
    if campaign.total_assignments > 0 and completed >= campaign.total_assignments:
        return True, "all_submitted"
    if campaign_is_due(campaign, now):
        return True, "deadline_reached"
    return False, None


def _aggregate(
    campaign: HumanReviewCampaign,
    assignments: list[HumanReviewAssignment],
    items: dict[UUID, HumanReviewItem],
) -> dict[str, Any]:
    submitted = [item for item in assignments if item.status == "submitted"]
    dimension_buckets: dict[str, list[float]] = {}
    overall_scores: list[float] = []
    reasons: list[dict[str, Any]] = []
    item_scores: dict[UUID, list[float]] = {}
    group_buckets: dict[str, dict[str, list[float]]] = {}
    for assignment in submitted:
        if assignment.overall_score is not None:
            score = float(assignment.overall_score)
            overall_scores.append(score)
            item_scores.setdefault(assignment.item_id, []).append(score)
            source_item = items.get(assignment.item_id)
            for key, value in (source_item.private_metadata if source_item else {}).items():
                if not isinstance(value, str | int | float | bool) or len(str(value)) > 100:
                    continue
                field = group_buckets.setdefault(str(key), {})
                label = str(value)
                if label in field or len(field) < 50:
                    field.setdefault(label, []).append(score)
        for dimension in assignment.dimension_scores:
            key = str(dimension.get("key") or "")
            score = dimension.get("score")
            if key and isinstance(score, int | float):
                dimension_buckets.setdefault(key, []).append(float(score))
        if assignment.reason:
            reasons.append(
                {
                    "sample": items.get(assignment.item_id).source_index + 1
                    if items.get(assignment.item_id)
                    else None,
                    "reason": assignment.reason[:1000],
                }
            )
    labels = {
        str(entry.get("key")): str(entry.get("label") or entry.get("key"))
        for entry in campaign.rubric
        if entry.get("key")
    }
    dimensions = {
        key: {
            "label": labels.get(key, key),
            "score": round(sum(values) / len(values), 2),
            "count": len(values),
        }
        for key, values in dimension_buckets.items()
        if values
    }
    per_item = [
        {
            "source_index": item.source_index,
            "prompt": item.prompt[:500],
            "average_score": round(sum(values) / len(values), 2),
            "review_count": len(values),
            "private_metadata": item.private_metadata,
        }
        for item_id, values in item_scores.items()
        if (item := items.get(item_id)) is not None and values
    ]
    group_averages = {
        key: {
            value: {"score": round(sum(scores) / len(scores), 2), "count": len(scores)}
            for value, scores in buckets.items()
            if scores
        }
        for key, buckets in group_buckets.items()
        if 1 < len(buckets) <= 50
    }
    return {
        "average_overall_score": (
            round(sum(overall_scores) / len(overall_scores), 2) if overall_scores else None
        ),
        "dimension_average_scores": dimensions,
        "submitted_assignments": len(submitted),
        "missing_assignments": max(campaign.total_assignments - len(submitted), 0),
        "completion_rate": round(len(submitted) / campaign.total_assignments, 4)
        if campaign.total_assignments
        else 0,
        "item_scores": per_item[:200],
        "item_scores_truncated": len(per_item) > 200,
        "group_average_scores": group_averages,
        "review_reasons": reasons[:200],
    }


def _fallback_markdown(campaign: HumanReviewCampaign, aggregate: dict[str, Any]) -> str:
    lines = [f"## {campaign.title}人工评审总结", ""]
    lines.append(
        f"共收到 **{aggregate['submitted_assignments']}** 份有效评分，"
        f"未提交 **{aggregate['missing_assignments']}** 份，完成率 "
        f"**{aggregate['completion_rate'] * 100:.1f}%**。"
    )
    if aggregate["average_overall_score"] is not None:
        lines.extend(["", f"综合平均分为 **{aggregate['average_overall_score']}/10**。"])
    dimensions = aggregate["dimension_average_scores"]
    if dimensions:
        lines.extend(
            ["", "### 各维度得分", "", "| 维度 | 平均分 | 有效评分数 |", "| --- | ---: | ---: |"]
        )
        lines.extend(
            f"| {value['label']} | {value['score']}/10 | {value['count']} |"
            for value in dimensions.values()
        )
    return "\n".join(lines)


def _ai_markdown(
    session: Session,
    campaign: HumanReviewCampaign,
    aggregate: dict[str, Any],
) -> str:
    job = session.get(AgentJob, campaign.job_id)
    model_id = job.input_config.get("evaluation_model_id") if job else None
    config = resolve_agent_config(session, model_id)
    if not config.enabled or not config.base_url or not config.model:
        return _fallback_markdown(campaign, aggregate)
    return request_text(
        config,
        (
            "你是人工评审结果分析师。请根据聚合统计、逐样本评分、匿名评语和隐藏分组元数据，"
            "生成面向任务发起人的中文 Markdown 总结。说明有效样本和未提交情况，比较评分维度，"
            "指出主要优缺点；如果元数据包含模型、角色或版本，请给出有数据支撑的分组比较。"
            "不得编造统计数字，不得披露评审者身份。正文简洁，不要输出 JSON。"
        ),
        {
            "campaign": {
                "title": campaign.title,
                "instructions": campaign.instructions,
                "completion_reason": campaign.completion_reason,
                "rubric": campaign.rubric,
            },
            "aggregate": aggregate,
        },
    )


def finalize_campaign(campaign_id: UUID, *, force: bool = False) -> bool:
    """Finalize once all reviews arrive or the deadline is reached."""
    with Session(get_engine()) as session:
        campaign = session.get(HumanReviewCampaign, campaign_id)
        if campaign is None or campaign.status in {"summarizing", "completed", "cancelled"}:
            return False
        assignments = list(
            session.exec(
                select(HumanReviewAssignment).where(
                    HumanReviewAssignment.campaign_id == campaign.id
                )
            ).all()
        )
        ready, reason = campaign_is_ready(campaign, assignments)
        if not ready and not force:
            return False
        campaign.completed_assignments = sum(item.status == "submitted" for item in assignments)
        campaign.status = "summarizing"
        campaign.summary_started_at = datetime.now(UTC)
        campaign.completion_reason = reason or "manual"
        campaign.updated_at = datetime.now(UTC)
        session.add(campaign)
        session.commit()

        review_items = list(
            session.exec(
                select(HumanReviewItem).where(HumanReviewItem.campaign_id == campaign.id)
            ).all()
        )
        job = session.get(AgentJob, campaign.job_id)
        aggregate = _aggregate(campaign, assignments, {item.id: item for item in review_items})
        try:
            markdown = _ai_markdown(session, campaign, aggregate)
            summary_mode = "ai"
        except Exception as error:
            markdown = _fallback_markdown(campaign, aggregate)
            aggregate["summary_error"] = str(error)[:1000]
            summary_mode = "fallback"
        session.refresh(campaign)
        if campaign.status == "cancelled":
            return False
        if job is not None:
            session.refresh(job)
            if job.status == AgentJobStatus.CANCELLED:
                campaign.status = "cancelled"
                campaign.completion_reason = "job_cancelled"
                campaign.completed_at = datetime.now(UTC)
                campaign.updated_at = datetime.now(UTC)
                session.add(campaign)
                session.commit()
                return False
        aggregate.update({"markdown": markdown, "summary_mode": summary_mode})
        if campaign.completion_reason == "deadline_reached":
            for assignment in assignments:
                if assignment.status == "pending":
                    assignment.status = "expired"
                    assignment.updated_at = datetime.now(UTC)
                    session.add(assignment)
        campaign.summary = aggregate
        campaign.status = "completed"
        campaign.completed_at = datetime.now(UTC)
        campaign.updated_at = datetime.now(UTC)
        session.add(campaign)
        if job is not None:
            job.status = AgentJobStatus.COMPLETED
            job.result = {
                **job.result,
                "human_review_campaign_id": str(campaign.id),
                "human_review_summary": aggregate,
            }
            job.updated_at = datetime.now(UTC)
            session.add(job)

        conversation = session.exec(
            select(AssistantConversation).where(
                AssistantConversation.agent_job_id == campaign.job_id,
                AssistantConversation.created_by == campaign.created_by,
            )
        ).first()
        if conversation is not None:
            session.add(
                AssistantMessage(
                    conversation_id=conversation.id,
                    role="assistant",
                    content=(f"{markdown}\n\n可前往“人工评审 → 我发起的”查看完整进度与汇总。"),
                )
            )
        session.commit()
        return True
