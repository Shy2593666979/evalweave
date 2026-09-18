from fastapi.testclient import TestClient


def test_registration_login_and_permissions(client: TestClient) -> None:
    options = client.get("/api/auth/registration-options")
    assert options.status_code == 200
    product = next(item for item in options.json()["data"] if item["code"] == "product")

    registration = client.post(
        "/api/auth/register",
        json={
            "username": "product_user",
            "email": "product_user@example.com",
            "password": "strong-password",
            "user_type_id": product["id"],
        },
    )
    assert registration.status_code == 201
    assert registration.json()["data"]["email"] == "product_user@example.com"
    assert registration.json()["data"]["user_type_name"] == "产品同学"

    login = client.post(
        "/api/auth/login",
        json={"username": "product_user", "password": "strong-password"},
    )
    assert login.status_code == 200
    assert client.get("/api/auth/me").json()["data"]["username"] == "product_user"
    assert client.post("/api/projects", json={"name": "Forbidden"}).status_code == 403


def test_registration_rejects_invalid_email(client: TestClient) -> None:
    options = client.get("/api/auth/registration-options").json()["data"]
    product = next(item for item in options if item["code"] == "product")
    response = client.post(
        "/api/auth/register",
        json={
            "username": "invalid_email_user",
            "email": "这不是邮箱",
            "password": "strong-password",
            "user_type_id": product["id"],
        },
    )
    assert response.status_code == 422


def test_admin_can_create_user_type_and_user(client: TestClient) -> None:
    login = client.post(
        "/api/auth/login",
        json={"username": "admin", "password": "test-admin-password"},
    )
    assert login.status_code == 200

    created_type = client.post(
        "/api/admin/user-types",
        json={
            "code": "operations",
            "name": "运营同学",
            "permissions": ["project:read", "experiment:read"],
            "selectable_on_registration": True,
        },
    )
    assert created_type.status_code == 201

    created_user = client.post(
        "/api/admin/users",
        json={
            "username": "operator",
            "email": "operator@example.com",
            "password": "operator-password",
            "system_role": "user",
            "user_type_id": created_type.json()["data"]["id"],
            "is_active": True,
        },
    )
    assert created_user.status_code == 201
    assert created_user.json()["data"]["user_type_name"] == "运营同学"


def test_admin_can_manage_evaluation_models_without_exposing_key(client: TestClient) -> None:
    client.post(
        "/api/auth/login",
        json={"username": "admin", "password": "test-admin-password"},
    )
    created = client.post(
        "/api/admin/evaluation-models",
        json={
            "name": "测试模型",
            "base_url": "https://model.example/v1/",
            "model_name": "evaluation-model",
            "api_mode": "responses",
            "api_key": "secret-key",
        },
    )
    assert created.status_code == 201
    assert created.json()["data"]["base_url"] == "https://model.example/v1"
    assert "api_key" not in created.json()["data"]

    listing = client.get("/api/evaluation-models")
    assert listing.status_code == 200
    assert listing.json()["data"][0]["name"] == "测试模型"
