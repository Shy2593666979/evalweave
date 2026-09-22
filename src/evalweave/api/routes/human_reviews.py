from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select

from evalweave.agents.inspection import load_source_rows
from evalweave.api.response import APIResponse
from evalweave.api.schemas.human_reviews import (
    ReviewCampaignCreate,
    ReviewCampaignFromFileCreate,
    ReviewItemCreate,
    ReviewSubmission,
)
from evalweave.auth.dependencies import CurrentUser, SessionDependency, require_permission
from evalweave.auth.permissions import Permission
from evalweave.core.config import get_settings
from evalweave.db.models import (
    AgentJob,
    AgentJobStatus,
    FileObject,
    HumanReviewAssignment,
    HumanReviewCampaign,
    HumanReviewItem,
    SystemRole,
    User,
    UserType,
)
from evalweave.human_reviews.service import campaign_is_due, finalize_campaign
from evalweave.storage import LocalFileStorage
from evalweave.workers.factory import create_celery_app

router = APIRouter(prefix="/human-reviews", tags=["human-reviews"])
ExperimentRunner = Annotated[User, Depends(require_permission(Permission.EXPERIMENT_RUN))]

def campaign_read(campaign: HumanReviewCampaign) -> dict[str, Any]:
    return {
        "id": campaign.id,
        "job_id": campaign.job_id,
        "created_by": campaign.created_by,
        "title": campaign.title,
        "instructions": campaign.instructions,
        "status": campaign.status,
        "rubric": campaign.rubric,
        "blind_config": campaign.blind_config,
        "item_count": campaign.item_count,
        "total_assignments": campaign.total_assignments,
        "completed_assignments": campaign.completed_assignments,
        "deadline_at": campaign.deadline_at,
        "completion_reason": campaign.completion_reason,
        "summary": campaign.summary,
        "completed_at": campaign.completed_at,
        "created_at": campaign.created_at,
        "updated_at": campaign.updated_at,
    }


def assignment_read(
    assignment: HumanReviewAssignment, item: HumanReviewItem, campaign: HumanReviewCampaign
) -> dict[str, Any]:
    return {
        "id": assignment.id,
        "campaign_id": assignment.campaign_id,
        "campaign_title": campaign.title,
        "campaign_instructions": campaign.instructions,
        "campaign_deadline_at": campaign.deadline_at,
        "item_id": assignment.item_id,
        "source_index": item.source_index,
        "prompt": item.prompt,
        "response": item.response,
        "metadata": item.visible_metadata,
        "rubric": campaign.rubric,
        "status": assignment.status,
        "dimension_scores": assignment.dimension_scores,
        "overall_score": assignment.overall_score,
        "reason": assignment.reason,
        "submitted_at": assignment.submitted_at,
        "created_at": assignment.created_at,
    }


def is_reviewer(session: Session, user: User) -> bool:
    if user.system_role == SystemRole.ADMIN:
        return True
    user_type = session.get(UserType, user.user_type_id) if user.user_type_id else None
    return bool(user_type and Permission.EVALUATION_REVIEW.value in user_type.permissions)


def require_campaign_owner(campaign_id: UUID, user: User, session: Session) -> HumanReviewCampaign:
    campaign = session.get(HumanReviewCampaign, campaign_id)
    if campaign is None or (
        campaign.created_by != user.id and user.system_role != SystemRole.ADMIN
    ):
        raise HTTPException(status_code=404, detail="人工评审任务不存在")
    return campaign


def _normalize_rubric(raw_rubric: list[dict[str, Any]]) -> list[dict[str, Any]]:
    keys: set[str] = set()
    normalized: list[dict[str, Any]] = []
    for raw in raw_rubric:
        key = str(raw.get("key") or "").strip()
        label = str(raw.get("label") or key).strip()
        minimum, maximum = float(raw.get("min_score", 1)), float(raw.get("max_score", 10))
        if not key or key in keys or minimum >= maximum:
            raise HTTPException(status_code=400, detail="评分维度配置无效")
        keys.add(key)
        normalized.append({"key": key, "label": label, "min_score": minimum, "max_score": maximum})
    return normalized


def enqueue_campaign_finalization(campaign_id: UUID, deadline_at: datetime | None = None) -> None:
    try:
        create_celery_app().send_task(
            "evalweave.human_reviews.finalize", args=[str(campaign_id)], eta=deadline_at
        )
    except Exception:
        return


@router.post("/campaigns", response_model=APIResponse[dict[str, Any]])
def create_campaign(
    payload: ReviewCampaignCreate, user: ExperimentRunner, session: SessionDependency
) -> APIResponse[dict[str, Any]]:
    job = session.get(AgentJob, payload.job_id)
    if job is None or (job.created_by != user.id and user.system_role != SystemRole.ADMIN):
        raise HTTPException(status_code=404, detail="评测任务不存在")
    reviewer_ids = list(dict.fromkeys(payload.reviewer_ids))
    reviewers = [session.get(User, reviewer_id) for reviewer_id in reviewer_ids]
    if any(reviewer is None or not reviewer.is_active for reviewer in reviewers):
        raise HTTPException(status_code=400, detail="评审员不存在或已停用")
    if any(not is_reviewer(session, reviewer) for reviewer in reviewers if reviewer):
        raise HTTPException(status_code=400, detail="所选用户缺少人工评审权限")
    if payload.reviews_per_item > len(reviewer_ids):
        raise HTTPException(status_code=400, detail="每条样本的评审人数不能超过评审员总数")
    deadline_at = (
        datetime.now(UTC) + timedelta(hours=payload.deadline_hours)
        if payload.deadline_hours
        else None
    )
    campaign = HumanReviewCampaign(
        job_id=job.id,
        created_by=user.id,
        title=payload.title.strip(),
        instructions=payload.instructions,
        rubric=_normalize_rubric(payload.rubric),
        blind_config=payload.blind_config,
        reviewer_ids=[str(item) for item in reviewer_ids],
        reviews_per_item=payload.reviews_per_item,
        item_count=len(payload.items),
        total_assignments=len(payload.items) * payload.reviews_per_item,
        deadline_at=deadline_at,
    )
    session.add(campaign)
    session.flush()
    job.status = AgentJobStatus.WAITING_HUMAN
    job.result = {**job.result, "human_review_campaign_id": str(campaign.id)}
    job.updated_at = datetime.now(UTC)
    session.add(job)
    for item_index, source in enumerate(payload.items):
        item = HumanReviewItem(
            campaign_id=campaign.id,
            source_index=item_index,
            prompt=source.prompt,
            response=source.response,
            visible_metadata=source.visible_metadata,
            private_metadata=source.private_metadata,
        )
        session.add(item)
        session.flush()
        for offset in range(payload.reviews_per_item):
            session.add(
                HumanReviewAssignment(
                    campaign_id=campaign.id,
                    item_id=item.id,
                    reviewer_id=reviewer_ids[(item_index + offset) % len(reviewer_ids)],
                )
            )
    session.commit()
    session.refresh(campaign)
    if deadline_at:
        enqueue_campaign_finalization(campaign.id, deadline_at)
    return APIResponse.success(campaign_read(campaign))


def _resolve_reviewer_ids(
    session: Session, type_codes: list[str], usernames: list[str]
) -> list[UUID]:
    codes = {item.strip().lower() for item in type_codes if item.strip()}
    names = {item.strip().lower() for item in usernames if item.strip()}
    known_types = (
        list(session.exec(select(UserType).where(UserType.code.in_(codes))).all()) if codes else []
    )
    missing_codes = codes - {item.code.lower() for item in known_types}
    if missing_codes:
        raise HTTPException(
            status_code=422, detail=f"用户类型不存在：{', '.join(sorted(missing_codes))}"
        )
    type_ids = {item.id for item in known_types}
    users = list(session.exec(select(User).where(User.is_active == True)).all())  # noqa: E712
    selected = [
        item for item in users if item.user_type_id in type_ids or item.username.lower() in names
    ]
    missing_names = names - {item.username.lower() for item in selected}
    if missing_names:
        raise HTTPException(
            status_code=422, detail=f"指定评审人不存在或已停用：{', '.join(sorted(missing_names))}"
        )
    invalid = [item.username for item in selected if not is_reviewer(session, item)]
    if invalid:
        raise HTTPException(status_code=422, detail=f"评审人缺少人工评审权限：{', '.join(invalid)}")
    return [item.id for item in selected]


def _find_column(
    row: dict[str, Any], requested: str | None, aliases: tuple[str, ...]
) -> str | None:
    lookup = {str(key).strip().lower(): str(key) for key in row}
    if requested and requested.strip().lower() in lookup:
        return lookup[requested.strip().lower()]
    return next((lookup[alias.lower()] for alias in aliases if alias.lower() in lookup), None)


@router.post("/campaigns/from-file", response_model=APIResponse[dict[str, Any]])
def create_campaign_from_file(
    payload: ReviewCampaignFromFileCreate, user: ExperimentRunner, session: SessionDependency
) -> APIResponse[dict[str, Any]]:
    job = session.get(AgentJob, payload.job_id)
    if job is None or (job.created_by != user.id and user.system_role != SystemRole.ADMIN):
        raise HTTPException(status_code=404, detail="评测任务不存在")
    source_id = payload.source_file_id or job.source_file_id
    source = session.get(FileObject, source_id) if source_id else None
    if source is None or source.project_id != job.project_id:
        raise HTTPException(status_code=422, detail="请选择当前项目中的 Excel 数据文件")
    if not source.original_name.lower().endswith(".xlsx"):
        raise HTTPException(status_code=422, detail="当前人工评审发布仅支持 XLSX 文件")
    reviewer_ids = _resolve_reviewer_ids(
        session, payload.reviewer_type_codes, payload.reviewer_usernames
    )
    if not reviewer_ids:
        raise HTTPException(status_code=422, detail="没有找到符合条件的评审人员")
    path = LocalFileStorage(get_settings().storage.local_directory).path_for(source.storage_key)
    rows = load_source_rows(path, source.original_name, payload.max_items)
    if not rows:
        raise HTTPException(status_code=422, detail="Excel 中没有可评审的数据行")
    query_column = _find_column(
        rows[0], payload.query_column, ("query", "prompt", "question", "问题")
    )
    answer_column = _find_column(
        rows[0], payload.answer_column, ("answer", "response", "output", "回复", "答案")
    )
    if not query_column or not answer_column:
        raise HTTPException(
            status_code=422,
            detail=f"找不到 query/answer 列；当前列为：{', '.join(map(str, rows[0]))}",
        )
    latency_column = _find_column(
        rows[0],
        payload.latency_column,
        (
            "latency_ms",
            "elapsed_ms",
            "duration_ms",
            "response_time_ms",
            "耗时（毫秒）",
            "耗时",
            "回复速度",
        ),
    )
    items: list[ReviewItemCreate] = []
    for row in rows:
        response = str(row.get(answer_column) or "").strip()
        if not response:
            continue
        visible_metadata = {"回复耗时": row.get(latency_column)} if latency_column else {}
        private_metadata = {
            str(key): value
            for key, value in row.items()
            if key not in {query_column, answer_column, latency_column}
        }
        items.append(
            ReviewItemCreate(
                prompt=str(row.get(query_column) or ""),
                response=response,
                visible_metadata=visible_metadata,
                private_metadata=private_metadata,
            )
        )
    if not items:
        raise HTTPException(status_code=422, detail="answer 列中没有可评审的回复")
    blind_config = {
        **payload.blind_config,
        "enabled": True,
        "source_file_id": str(source.id),
        "query_column": query_column,
        "answer_column": answer_column,
        "latency_column": latency_column,
        "evidence_warnings": []
        if latency_column
        else ["源文件没有可识别的回复耗时列，速度评分缺少客观耗时证据"],
    }
    return create_campaign(
        ReviewCampaignCreate(
            job_id=job.id,
            title=payload.title,
            instructions=payload.instructions,
            reviewer_ids=reviewer_ids,
            reviews_per_item=len(reviewer_ids),
            rubric=payload.rubric,
            blind_config=blind_config,
            items=items,
            deadline_hours=payload.deadline_hours,
        ),
        user,
        session,
    )


def _enqueue_expired(campaigns: list[HumanReviewCampaign]) -> None:
    for campaign in campaigns:
        if campaign.status == "active" and campaign_is_due(campaign):
            enqueue_campaign_finalization(campaign.id)


@router.get(
    "/assignments/mine",
    response_model=APIResponse[list[dict[str, Any]]],
)
def list_my_assignments(
    user: CurrentUser, session: SessionDependency
) -> APIResponse[list[dict[str, Any]]]:
    _enqueue_expired(
        list(
            session.exec(
                select(HumanReviewCampaign).where(HumanReviewCampaign.status == "active")
            ).all()
        )
    )
    rows = session.exec(
        select(HumanReviewAssignment, HumanReviewItem, HumanReviewCampaign)
        .join(HumanReviewItem, HumanReviewItem.id == HumanReviewAssignment.item_id)
        .join(HumanReviewCampaign, HumanReviewCampaign.id == HumanReviewAssignment.campaign_id)
        .where(HumanReviewAssignment.reviewer_id == user.id)
        .order_by(HumanReviewAssignment.status, HumanReviewAssignment.created_at)
    ).all()
    return APIResponse.success(
        [assignment_read(assignment, item, campaign) for assignment, item, campaign in rows]
    )


@router.get("/campaigns/mine", response_model=APIResponse[list[dict[str, Any]]])
def list_my_campaigns(
    user: CurrentUser, session: SessionDependency
) -> APIResponse[list[dict[str, Any]]]:
    campaigns = list(
        session.exec(
            select(HumanReviewCampaign)
            .where(HumanReviewCampaign.created_by == user.id)
            .order_by(HumanReviewCampaign.created_at.desc())
        ).all()
    )
    _enqueue_expired(campaigns)
    return APIResponse.success([campaign_read(item) for item in campaigns])


def update_campaign_progress(session: Session, campaign: HumanReviewCampaign) -> bool:
    assignments = list(
        session.exec(
            select(HumanReviewAssignment).where(HumanReviewAssignment.campaign_id == campaign.id)
        ).all()
    )
    campaign.completed_assignments = sum(item.status == "submitted" for item in assignments)
    campaign.updated_at = datetime.now(UTC)
    session.add(campaign)
    return campaign.completed_assignments >= campaign.total_assignments


@router.post(
    "/assignments/{assignment_id}/submit",
    response_model=APIResponse[dict[str, Any]],
)
def submit_review(
    assignment_id: UUID, payload: ReviewSubmission, user: CurrentUser, session: SessionDependency
) -> APIResponse[dict[str, Any]]:
    assignment = session.get(HumanReviewAssignment, assignment_id)
    if assignment is None or assignment.reviewer_id != user.id:
        raise HTTPException(status_code=404, detail="待评样本不存在")
    if assignment.status == "submitted":
        raise HTTPException(status_code=409, detail="该样本已经提交，不能重复评分")
    campaign = session.get(HumanReviewCampaign, assignment.campaign_id)
    item = session.get(HumanReviewItem, assignment.item_id)
    if campaign is None or item is None or campaign.status != "active":
        raise HTTPException(status_code=409, detail="人工评审任务当前不可提交")
    if campaign_is_due(campaign):
        enqueue_campaign_finalization(campaign.id)
        raise HTTPException(status_code=409, detail="人工评审已到截止时间")
    rubric = {str(entry.get("key")): entry for entry in campaign.rubric}
    submitted = {entry.key: entry.score for entry in payload.dimension_scores}
    if set(submitted) != set(rubric):
        raise HTTPException(status_code=400, detail="请完成所有评分维度")
    normalized: list[dict[str, Any]] = []
    for key, value in submitted.items():
        rule = rubric[key]
        minimum, maximum = float(rule.get("min_score", 1)), float(rule.get("max_score", 10))
        if value < minimum or value > maximum:
            raise HTTPException(status_code=400, detail=f"{rule.get('label', key)}评分超出范围")
        normalized.append({"key": key, "label": rule.get("label", key), "score": value})
    overall = (
        payload.overall_score
        if payload.overall_score is not None
        else round(sum(submitted.values()) / len(submitted), 2)
    )
    if overall < 1 or overall > 10:
        raise HTTPException(status_code=400, detail="综合评分必须在 1 到 10 之间")
    assignment.dimension_scores, assignment.overall_score, assignment.reason = (
        normalized,
        overall,
        payload.reason,
    )
    assignment.status, assignment.submitted_at, assignment.updated_at = (
        "submitted",
        datetime.now(UTC),
        datetime.now(UTC),
    )
    session.add(assignment)
    all_submitted = update_campaign_progress(session, campaign)
    session.commit()
    session.refresh(assignment)
    if all_submitted:
        finalize_campaign(campaign.id)
    return APIResponse.success(assignment_read(assignment, item, campaign))
