from fastapi.testclient import TestClient


def test_admin_creates_and_assigns_project(client: TestClient) -> None:
    options = client.get("/api/auth/registration-options").json()["data"]
    development = next(item for item in options if item["code"] == "development")
    client.post(
        "/api/auth/register",
        json={
            "username": "developer",
            "email": "developer@example.com",
            "password": "developer-password",
            "user_type_id": development["id"],
        },
    )
    login_response = client.post(
        "/api/auth/login",
        json={"username": "developer", "password": "developer-password"},
    )
    assert login_response.status_code == 200

    assert client.post("/api/projects", json={"name": "Forbidden"}).status_code == 403
    default_projects = client.get("/api/projects").json()["data"]
    assert len(default_projects) == 1
    developer_id = client.get("/api/auth/me").json()["data"]["id"]

    client.post("/api/auth/logout")
    admin_login = client.post(
        "/api/auth/login",
        json={"username": "admin", "password": "test-admin-password"},
    )
    assert admin_login.status_code == 200
    create_response = client.post(
        "/api/projects",
        json={
            "name": "Companion Evaluation",
            "description": "Evaluation workspace",
            "service_url": "https://companion.example.com",
            "agent_context": "面向陪伴场景的对话评测",
        },
    )
    assert create_response.status_code == 201
    project = create_response.json()["data"]
    assert project["name"] == "Companion Evaluation"
    assert project["service_url"] == "https://companion.example.com"
    assigned = client.put(
        f"/api/projects/{project['id']}/members",
        json={"user_ids": [developer_id]},
    )
    assert assigned.status_code == 200
    reassigned = client.put(
        f"/api/projects/{project['id']}/members",
        json={"user_ids": [developer_id]},
    )
    assert reassigned.status_code == 200
    assert reassigned.json()["data"] == [developer_id]

    client.post("/api/auth/logout")
    client.post(
        "/api/auth/login",
        json={"username": "developer", "password": "developer-password"},
    )
    list_response = client.get("/api/projects")
    assert list_response.status_code == 200
    assert {item["id"] for item in list_response.json()["data"]} == {
        default_projects[0]["id"],
        project["id"],
    }
