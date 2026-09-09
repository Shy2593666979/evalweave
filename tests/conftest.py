from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlmodel import SQLModel

from evalweave.core.config import set_config_path
from evalweave.db.session import get_engine


@pytest.fixture
def client() -> TestClient:
    set_config_path(Path("config/application.test.yaml"))
    engine = get_engine()
    SQLModel.metadata.create_all(engine)
    from evalweave.main import create_app

    with TestClient(create_app()) as test_client:
        yield test_client
    SQLModel.metadata.drop_all(engine)
