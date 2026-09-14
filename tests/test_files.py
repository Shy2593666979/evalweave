import hashlib

from fastapi.testclient import TestClient


def login_as_developer(client: TestClient) -> None:
    options = client.get("/api/auth/registration-options").json()
    development = next(item for item in options if item["code"] == "development")
    client.post(
        "/api/auth/register",
        json={
            "username": "file_developer",
            "email": "file_developer@example.com",
            "password": "developer-password",
            "user_type_id": development["id"],
        },
    )
    response = client.post(
        "/api/auth/login",
        json={"username": "file_developer", "password": "developer-password"},
    )
    assert response.status_code == 200


def create_project(client: TestClient) -> str:
    response = client.post("/api/projects", json={"name": "File storage test"})
    assert response.status_code == 201
    return response.json()["id"]


def test_upload_list_download_and_delete_file(client: TestClient) -> None:
    login_as_developer(client)
    project_id = create_project(client)
    content = b'{"message": "hello"}'

    upload = client.post(
        f"/api/projects/{project_id}/files",
        files={"file": ("cases.json", content, "application/json")},
        data={"category": "dataset_source"},
    )
    assert upload.status_code == 201
    metadata = upload.json()
    assert metadata["original_name"] == "cases.json"
    assert metadata["size_bytes"] == len(content)
    assert metadata["sha256"] == hashlib.sha256(content).hexdigest()
    assert "storage_key" not in metadata

    listing = client.get(f"/api/projects/{project_id}/files")
    assert listing.status_code == 200
    assert [item["id"] for item in listing.json()] == [metadata["id"]]

    download = client.get(f"/api/files/{metadata['id']}/content")
    assert download.status_code == 200
    assert download.content == content

    deletion = client.delete(f"/api/files/{metadata['id']}")
    assert deletion.status_code == 204
    assert client.get(f"/api/files/{metadata['id']}").status_code == 404


def test_rejects_unsupported_dataset_extension(client: TestClient) -> None:
    login_as_developer(client)
    project_id = create_project(client)
    response = client.post(
        f"/api/projects/{project_id}/files",
        files={"file": ("payload.exe", b"not executable", "application/octet-stream")},
    )
    assert response.status_code == 422
