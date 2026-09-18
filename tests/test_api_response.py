from fastapi.testclient import TestClient

from evalweave.api.response import APIResponse


def test_api_response_builds_success_envelope() -> None:
    assert APIResponse.success({"id": "1"}).model_dump() == {
        "code": 0,
        "message": "操作成功",
        "data": {"id": "1"},
    }


def test_api_response_builds_failure_envelope() -> None:
    assert APIResponse.fail("项目不存在", code=404).model_dump() == {
        "code": 404,
        "message": "项目不存在",
        "data": None,
    }


def test_service_error_is_rendered_by_api_layer(client: TestClient) -> None:
    login = client.post(
        "/api/auth/login",
        json={"username": "admin", "password": "test-admin-password"},
    )
    assert login.status_code == 200

    response = client.get("/api/projects/00000000-0000-0000-0000-000000000000")

    assert response.status_code == 404
    assert response.json() == {
        "code": 404,
        "message": "项目不存在",
        "data": None,
    }
