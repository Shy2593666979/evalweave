from fastapi.testclient import TestClient


def test_registration_login_and_permissions(client: TestClient) -> None:
    options = client.get("/api/auth/registration-options")
    assert options.status_code == 200
    product = next(item for item in options.json() if item["code"] == "product")

    registration = client.post(
        "/api/auth/register",
        json={
            "username": "product_user",
            "password": "strong-password",
            "user_type_id": product["id"],
        },
    )
    assert registration.status_code == 201
    assert registration.json()["user_type_name"] == "产品同学"

    login = client.post(
        "/api/auth/login",
        json={"username": "product_user", "password": "strong-password"},
    )
    assert login.status_code == 200
    assert client.get("/api/auth/me").json()["username"] == "product_user"
    assert client.post("/api/projects", json={"name": "Forbidden"}).status_code == 403


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
            "password": "operator-password",
            "system_role": "user",
            "user_type_id": created_type.json()["id"],
            "is_active": True,
        },
    )
    assert created_user.status_code == 201
    assert created_user.json()["user_type_name"] == "运营同学"
