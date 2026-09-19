from datetime import UTC, datetime, timedelta
from uuid import UUID

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from evalweave.db.models import AgentJob, AgentJobStatus, EvaluationSchedule
from evalweave.db.session import get_engine
from evalweave.services.scheduled_evaluations import (
    dispatch_due_schedules,
    next_run_time,
)


def login_admin(client: TestClient) -> None:
    response = client.post(
        "/api/auth/login",
        json={"username": "admin", "password": "test-admin-password"},
    )
    assert response.status_code == 200


def test_next_run_time_supports_daily_and_weekly_rules() -> None:
    after = datetime(2026, 9, 19, 1, 0, tzinfo=UTC)  # Saturday 09:00 in Shanghai.
    daily = next_run_time(
        {"type": "daily", "time": "10:30", "weekdays": []},
        "Asia/Shanghai",
        after,
    )
    weekly = next_run_time(
        {"type": "weekly", "time": "09:30", "weekdays": [0, 2]},
        "Asia/Shanghai",
        after,
    )
    assert daily == datetime(2026, 9, 19, 2, 30, tzinfo=UTC)
    assert weekly == datetime(2026, 9, 21, 1, 30, tzinfo=UTC)


def test_completed_result_can_create_a_scheduled_evaluation(client: TestClient) -> None:
    login_admin(client)
    project = client.get("/api/projects").json()["data"][0]
    created = client.post(
        f"/api/projects/{project['id']}/agent-jobs",
        json={"title": "Preview", "goal": "Evaluate answers", "input_config": {}},
    ).json()["data"]
    with Session(get_engine()) as session:
        job = session.get(AgentJob, UUID(created["id"]))
        assert job is not None
        job.status = AgentJobStatus.COMPLETED
        job.eval_spec = {"operations": ["evaluate"], "source": {"case_count": 5}}
        job.result = {"summary": "4 of 5 passed", "average_score": 8.2}
        session.add(job)
        session.commit()

    response = client.post(
        f"/api/projects/{project['id']}/scheduled-evaluations",
        json={
            "source_job_id": created["id"],
            "name": "Daily quality check",
            "recurrence": {"type": "daily", "time": "09:00", "weekdays": []},
            "timezone": "Asia/Shanghai",
        },
    )
    assert response.status_code == 201
    scheduled_evaluation = response.json()["data"]
    schedule = scheduled_evaluation["schedule"]
    assert schedule["recurrence"] == {
        "type": "daily",
        "time": "09:00",
        "weekdays": [],
    }

    with Session(get_engine()) as session:
        stored = session.get(EvaluationSchedule, UUID(schedule["id"]))
        assert stored is not None
        jobs = dispatch_due_schedules(session, stored.next_run_at + timedelta(seconds=1))
        assert len(jobs) == 1
        generated = session.exec(select(AgentJob).where(AgentJob.id == jobs[0])).one()
        assert generated.trigger_type == "scheduled"
        assert generated.eval_spec["source"]["case_count"] == 5
        assert generated.scheduled_evaluation_id == UUID(
            schedule["scheduled_evaluation_id"]
        )
        assert generated.snapshot_id == UUID(schedule["snapshot_id"])
        assert dispatch_due_schedules(session, stored.next_run_at - timedelta(seconds=1)) == []


def test_unfinished_job_cannot_be_used_as_preview(client: TestClient) -> None:
    login_admin(client)
    project = client.get("/api/projects").json()["data"][0]
    job = client.post(
        f"/api/projects/{project['id']}/agent-jobs",
        json={"title": "Pending", "goal": "Not ready"},
    ).json()["data"]
    response = client.post(
        f"/api/projects/{project['id']}/scheduled-evaluations",
        json={
            "source_job_id": job["id"],
            "name": "Invalid plan",
            "recurrence": {"type": "daily", "time": "09:00", "weekdays": []},
        },
    )
    assert response.status_code == 409


def test_scheduled_evaluation_crud_and_run_now(client: TestClient, monkeypatch) -> None:
    login_admin(client)
    project = client.get("/api/projects").json()["data"][0]
    created = client.post(
        f"/api/projects/{project['id']}/agent-jobs",
        json={
            "title": "Reusable quality check",
            "goal": "Evaluate reusable answers",
            "input_config": {"output_format": "xlsx"},
        },
    ).json()["data"]
    with Session(get_engine()) as session:
        job = session.get(AgentJob, UUID(created["id"]))
        assert job is not None
        job.status = AgentJobStatus.COMPLETED
        job.eval_spec = {"operations": ["evaluate"], "source": {"case_count": 3}}
        session.add(job)
        session.commit()

    response = client.post(
        f"/api/projects/{project['id']}/scheduled-evaluations",
        json={
            "source_job_id": created["id"],
            "name": "Daily regression",
            "description": "Run the validated evaluation every day",
            "recurrence": {"type": "daily", "time": "09:00", "weekdays": []},
            "timezone": "Asia/Shanghai",
        },
    )
    assert response.status_code == 201
    scheduled = response.json()["data"]
    schedule_id = scheduled["schedule"]["id"]
    assert scheduled["schedule"]["status"] == "enabled"
    assert scheduled["output_format"] == "xlsx"
    assert scheduled["recent_runs"] == []

    listed = client.get(
        f"/api/projects/{project['id']}/scheduled-evaluations"
    ).json()["data"]
    assert [item["schedule"]["id"] for item in listed] == [schedule_id]

    updated = client.put(
        f"/api/schedules/{schedule_id}",
        json={
            "name": "Weekly regression",
            "recurrence": {"type": "weekly", "time": "10:30", "weekdays": [0, 2]},
            "timezone": "Asia/Shanghai",
            "misfire_policy": "skip",
        },
    )
    assert updated.status_code == 200
    assert updated.json()["data"]["recurrence"]["weekdays"] == [0, 2]

    current = datetime.now(UTC)
    with Session(get_engine()) as session:
        stored = session.get(EvaluationSchedule, UUID(schedule_id))
        assert stored is not None
        stored.next_run_at = current - timedelta(minutes=5)
        session.add(stored)
        session.commit()
        assert dispatch_due_schedules(session, current) == []
        session.refresh(stored)
        assert stored.last_run_at is None
        assert stored.next_run_at.replace(tzinfo=UTC) > current

    dispatched: list[UUID] = []
    monkeypatch.setattr(
        "evalweave.api.routes.scheduled_evaluations.enqueue_scheduled_job",
        lambda job: dispatched.append(job.id),
    )
    run = client.post(f"/api/schedules/{schedule_id}/run-now")
    assert run.status_code == 200
    assert run.json()["data"]["trigger_type"] == "scheduled"
    assert dispatched == [UUID(run.json()["data"]["id"])]

    current = datetime.now(UTC)
    with Session(get_engine()) as session:
        stored = session.get(EvaluationSchedule, UUID(schedule_id))
        assert stored is not None
        stored.misfire_policy = "latest"
        stored.next_run_at = current - timedelta(seconds=1)
        session.add(stored)
        session.commit()
        assert dispatch_due_schedules(session, current) == []
        session.refresh(stored)
        assert stored.last_run_at is None

    paused = client.patch(
        f"/api/schedules/{schedule_id}/enabled", json={"enabled": False}
    )
    assert paused.status_code == 200
    assert paused.json()["data"]["status"] == "paused"

    removed = client.delete(f"/api/schedules/{schedule_id}")
    assert removed.status_code == 200
    assert (
        client.get(f"/api/projects/{project['id']}/scheduled-evaluations").json()[
            "data"
        ]
        == []
    )
