from uuid import UUID

from fastapi.testclient import TestClient

from evalweave.agents.workflow import execute_agent_job, plan_agent_job
from evalweave.core.config import get_settings


def login_as_developer(client: TestClient) -> None:
    options = client.get("/api/auth/registration-options").json()
    development = next(item for item in options if item["code"] == "development")
    client.post(
        "/api/auth/register",
        json={
            "username": "agent_developer",
            "password": "developer-password",
            "user_type_id": development["id"],
        },
    )
    response = client.post(
        "/api/auth/login",
        json={"username": "agent_developer", "password": "developer-password"},
    )
    assert response.status_code == 200


def test_agent_plans_waits_for_human_and_resumes(client: TestClient, monkeypatch) -> None:
    login_as_developer(client)
    project = client.post("/api/projects", json={"name": "Agent workflow"}).json()
    upload = client.post(
        f"/api/projects/{project['id']}/files",
        files={
            "file": (
                "companion.json",
                b'[{"user_message":"I feel lonely","scene":"support"}]',
                "application/json",
            )
        },
    )
    assert upload.status_code == 201

    created = client.post(
        f"/api/projects/{project['id']}/agent-jobs",
        json={
            "title": "Companion evaluation",
            "goal": "Evaluate conversation quality, tools and latency",
            "source_file_id": upload.json()["id"],
            "notification_targets": [
                {"channel": "email", "recipient": "reviewer@example.com"}
            ],
        },
    )
    assert created.status_code == 201
    job_id = UUID(created.json()["id"])

    plan_agent_job(job_id)
    planned = client.get(f"/api/agent-jobs/{job_id}").json()
    assert planned["status"] == "waiting_human"
    assert planned["eval_spec"]["source"]["fields"] == ["scene", "user_message"]

    steps = client.get(f"/api/agent-jobs/{job_id}/steps").json()
    assert [step["name"] for step in steps] == ["discover_source", "generate_eval_spec"]

    tasks = client.get("/api/human-tasks?task_status=pending").json()
    assert len(tasks) == 1
    deliveries = client.get(f"/api/human-tasks/{tasks[0]['id']}/deliveries").json()
    assert deliveries[0]["status"] == "failed"
    assert "not configured" in deliveries[0]["error"]

    queued: list[tuple[str, UUID]] = []
    monkeypatch.setattr(
        "evalweave.api.routes.agents.enqueue_agent_task",
        lambda name, identifier: queued.append((name, identifier)),
    )
    decision = client.post(
        f"/api/human-tasks/{tasks[0]['id']}/decision",
        json={"decision": "approve", "reason": "Plan looks good"},
    )
    assert decision.status_code == 200
    assert decision.json()["status"] == "approved"
    assert queued == [("evalweave.agent.execute", job_id)]

    execute_agent_job(job_id)
    completed = client.get(f"/api/agent-jobs/{job_id}").json()
    assert completed["status"] == "completed"
    assert completed["result"]["execution"]["execution_mode"] == "declarative"


def test_agent_executes_allowlisted_http_target(client: TestClient, monkeypatch) -> None:
    class FakeResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self):
            return {"data": {"reply": "I hear you"}}

    class FakeClient:
        def __init__(self, **_):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

        def post(self, *_args, **_kwargs):
            return FakeResponse()

    login_as_developer(client)
    project = client.post("/api/projects", json={"name": "Target execution"}).json()
    upload = client.post(
        f"/api/projects/{project['id']}/files",
        files={"file": ("cases.json", b'[{"message":"hello"}]', "application/json")},
    ).json()
    settings = get_settings()
    original_hosts = list(settings.agent.allowed_target_hosts)
    settings.agent.allowed_target_hosts = ["model.test"]
    monkeypatch.setattr("evalweave.agents.executor.httpx.Client", FakeClient)
    try:
        created = client.post(
            f"/api/projects/{project['id']}/agent-jobs",
            json={
                "title": "HTTP target",
                "goal": "Run target",
                "source_file_id": upload["id"],
                "requires_approval": False,
                "input_config": {
                    "target": {
                        "url": "https://model.test/chat",
                        "body": {"message": "{{message}}"},
                        "response_path": "data.reply",
                    }
                },
            },
        ).json()
        plan_agent_job(UUID(created["id"]))
    finally:
        settings.agent.allowed_target_hosts = original_hosts

    completed = client.get(f"/api/agent-jobs/{created['id']}").json()
    target = completed["result"]["execution"]["target"]
    assert target["executed"] is True
    assert target["succeeded"] == 1
    result_file = client.get(f"/api/files/{target['result_file_id']}/content")
    assert result_file.status_code == 200
    assert b'I hear you' in result_file.content
