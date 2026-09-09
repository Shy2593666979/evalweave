from fastapi.testclient import TestClient


def test_create_and_list_project(client: TestClient) -> None:
    options = client.get("/api/auth/registration-options").json()
    development = next(item for item in options if item["code"] == "development")
    client.post(
        "/api/auth/register",
        json={
            "username": "developer",
            "password": "developer-password",
            "user_type_id": development["id"],
        },
    )
    login_response = client.post(
        "/api/auth/login",
        json={"username": "developer", "password": "developer-password"},
    )
    assert login_response.status_code == 200

    create_response = client.post(
        "/api/projects",
        json={"name": "Companion Evaluation", "description": "Evaluation workspace"},
    )
    assert create_response.status_code == 201
    project = create_response.json()
    assert project["name"] == "Companion Evaluation"

    list_response = client.get("/api/projects")
    assert list_response.status_code == 200
    assert [item["id"] for item in list_response.json()] == [project["id"]]
