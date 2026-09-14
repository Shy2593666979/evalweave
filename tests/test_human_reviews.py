from io import BytesIO

from fastapi.testclient import TestClient
from openpyxl import Workbook


def register_and_login(client: TestClient, username: str, user_type_code: str) -> dict:
    options = client.get("/api/auth/registration-options").json()
    user_type = next(item for item in options if item["code"] == user_type_code)
    registered = client.post(
        "/api/auth/register",
        json={
            "username": username,
            "email": f"{username}@example.com",
            "password": "review-password",
            "user_type_id": user_type["id"],
        },
    )
    assert registered.status_code == 201
    login = client.post(
        "/api/auth/login",
        json={"username": username, "password": "review-password"},
    )
    assert login.status_code == 200
    return registered.json()


def test_human_review_assignment_is_private_and_aggregates(client: TestClient) -> None:
    initiator = register_and_login(client, "review_initiator", "development")
    project = client.post("/api/projects", json={"name": "Human review"}).json()
    job = client.post(
        f"/api/projects/{project['id']}/agent-jobs",
        json={"title": "Blind review source", "goal": "Compare response quality"},
    ).json()

    reviewer = register_and_login(client, "assigned_reviewer", "research")
    client.post(
        "/api/auth/login",
        json={"username": initiator["username"], "password": "review-password"},
    )
    created = client.post(
        "/api/human-reviews/campaigns",
        json={
            "job_id": job["id"],
            "title": "客服回答匿名评审",
            "instructions": "根据帮助程度评分",
            "reviewer_ids": [reviewer["id"]],
            "rubric": [
                {"key": "quality", "label": "质量", "min_score": 1, "max_score": 10},
                {"key": "relevance", "label": "相关性", "min_score": 1, "max_score": 10},
            ],
            "items": [
                {
                    "prompt": "怎么退款？",
                    "response": "请在订单详情中申请退款。",
                    "visible_metadata": {"scene": "售后"},
                    "private_metadata": {"model": "secret-model"},
                }
            ],
        },
    )
    assert created.status_code == 200
    assert created.json()["total_assignments"] == 1

    client.post(
        "/api/auth/login",
        json={"username": reviewer["username"], "password": "review-password"},
    )
    assignments = client.get("/api/human-reviews/assignments/mine")
    assert assignments.status_code == 200
    assignment = assignments.json()[0]
    assert assignment["metadata"] == {"scene": "售后"}
    assert "secret-model" not in str(assignment)

    submitted = client.post(
        f"/api/human-reviews/assignments/{assignment['id']}/submit",
        json={
            "dimension_scores": [
                {"key": "quality", "score": 8},
                {"key": "relevance", "score": 9},
            ],
            "reason": "回答直接且相关",
        },
    )
    assert submitted.status_code == 200
    assert submitted.json()["overall_score"] == 8.5
    assert (
        client.post(
            f"/api/human-reviews/assignments/{assignment['id']}/submit",
            json={"dimension_scores": [{"key": "quality", "score": 1}]},
        ).status_code
        == 409
    )

    client.post(
        "/api/auth/login",
        json={"username": initiator["username"], "password": "review-password"},
    )
    campaigns = client.get("/api/human-reviews/campaigns/mine").json()
    assert campaigns[0]["status"] == "completed"
    assert campaigns[0]["completed_assignments"] == 1
    assert campaigns[0]["summary"]["average_overall_score"] == 8.5


def test_create_human_review_from_excel_for_groups_and_named_user(
    client: TestClient, monkeypatch
) -> None:
    monkeypatch.setattr(
        "evalweave.api.routes.human_reviews.enqueue_campaign_finalization",
        lambda *_args, **_kwargs: None,
    )
    initiator = register_and_login(client, "file_review_owner", "development")
    project = client.post("/api/projects", json={"name": "Excel review"}).json()

    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["query", "answer", "latency_ms", "model"])
    sheet.append(["1+1等于几", "2", 320, "model-a"])
    sheet.append(["请介绍春天", "春天万物复苏。", 860, "model-b"])
    content = BytesIO()
    workbook.save(content)
    uploaded = client.post(
        f"/api/projects/{project['id']}/files",
        files={
            "file": (
                "reviews.xlsx",
                content.getvalue(),
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        },
        data={"category": "dataset_source"},
    ).json()
    job = client.post(
        f"/api/projects/{project['id']}/agent-jobs",
        json={
            "title": "人工评审来源",
            "goal": "评审 Excel 回复",
            "source_file_id": uploaded["id"],
        },
    ).json()

    register_and_login(client, "product_reviewer", "product")
    register_and_login(client, "research_reviewer", "research")
    named = register_and_login(client, "tmg", "development")
    client.post(
        "/api/auth/login",
        json={"username": initiator["username"], "password": "review-password"},
    )
    created = client.post(
        "/api/human-reviews/campaigns/from-file",
        json={
            "job_id": job["id"],
            "title": "Excel 回复人工评审",
            "instructions": "按回复速度和效果分别打分",
            "reviewer_type_codes": ["product", "research"],
            "reviewer_usernames": ["tmg"],
            "deadline_hours": 24,
        },
    )
    assert created.status_code == 200
    campaign = created.json()
    assert campaign["item_count"] == 2
    assert campaign["total_assignments"] == 6
    assert campaign["deadline_at"] is not None
    assert campaign["blind_config"]["latency_column"] == "latency_ms"

    client.post(
        "/api/auth/login",
        json={"username": named["username"], "password": "review-password"},
    )
    assignments = client.get("/api/human-reviews/assignments/mine").json()
    own = [item for item in assignments if item["campaign_id"] == campaign["id"]]
    assert len(own) == 2
    assert own[0]["metadata"]["回复耗时"] in {320, 860}
    assert "model-a" not in str(own)
