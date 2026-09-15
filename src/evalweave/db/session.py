from collections.abc import Generator
from functools import lru_cache
from pathlib import Path

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.engine.url import make_url
from sqlmodel import Session, SQLModel, create_engine

import evalweave.db.models  # noqa: F401
from evalweave.core.config import get_settings


@lru_cache(maxsize=1)
def get_engine() -> Engine:
    config = get_settings().database
    options: dict[str, object] = {"echo": config.echo, "pool_pre_ping": True}
    if config.url.startswith("sqlite"):
        database_path = make_url(config.url).database
        if database_path and database_path != ":memory:":
            Path(database_path).parent.mkdir(parents=True, exist_ok=True)
    else:
        options.update(
            pool_size=config.pool_size,
            max_overflow=config.max_overflow,
            pool_recycle=config.pool_recycle,
        )
    return create_engine(config.url, **options)


def get_session() -> Generator[Session, None, None]:
    with Session(get_engine()) as session:
        yield session


def create_db_and_tables() -> None:
    engine = get_engine()
    SQLModel.metadata.create_all(engine)
    _ensure_user_email_column(engine)
    _ensure_assistant_message_attachment_columns(engine)
    _ensure_human_review_campaign_columns(engine)


def _ensure_user_email_column(engine: Engine) -> None:
    """Add user email support to installations without Alembic."""
    inspector = inspect(engine)
    if "users" not in inspector.get_table_names():
        return
    existing = {column["name"] for column in inspector.get_columns("users")}
    with engine.begin() as connection:
        if "email" not in existing:
            connection.execute(text("ALTER TABLE users ADD COLUMN email VARCHAR(255) NULL"))
    inspector = inspect(engine)
    indexes = {index["name"] for index in inspector.get_indexes("users")}
    if "ix_users_email" not in indexes:
        with engine.begin() as connection:
            connection.execute(text("CREATE UNIQUE INDEX ix_users_email ON users (email)"))


def _ensure_assistant_message_attachment_columns(engine: Engine) -> None:
    """Keep installations without Alembic compatible with attachment-aware messages."""
    inspector = inspect(engine)
    if "assistant_messages" not in inspector.get_table_names():
        return
    existing = {column["name"] for column in inspector.get_columns("assistant_messages")}
    file_id_column = next(
        (
            column
            for column in inspector.get_columns("file_objects")
            if column["name"] == "id"
        ),
        None,
    )
    file_id_type = (
        file_id_column["type"].compile(dialect=engine.dialect)
        if file_id_column is not None
        else "CHAR(32)"
    )
    additions = {
        "ui_action": "JSON",
        "include_in_context": "BOOLEAN NOT NULL DEFAULT 1",
        "is_streaming": "BOOLEAN NOT NULL DEFAULT 0",
        "attachment_file_id": file_id_type,
        "attachment_name": "VARCHAR(255)",
        "attachment_content_type": "VARCHAR(255)",
        "attachment_size_bytes": "INTEGER",
    }
    with engine.begin() as connection:
        for name, column_type in additions.items():
            if name not in existing:
                connection.execute(
                    text(
                        f"ALTER TABLE assistant_messages ADD COLUMN {name} "
                        f"{column_type} NULL"
                    )
                )


def _ensure_human_review_campaign_columns(engine: Engine) -> None:
    """Add deadline/finalization fields for installations without Alembic."""
    inspector = inspect(engine)
    if "human_review_campaigns" not in inspector.get_table_names():
        return
    existing = {
        column["name"] for column in inspector.get_columns("human_review_campaigns")
    }
    additions = {
        "deadline_at": "DATETIME NULL",
        "summary_started_at": "DATETIME NULL",
        "completion_reason": "VARCHAR(32) NULL",
    }
    with engine.begin() as connection:
        for name, definition in additions.items():
            if name not in existing:
                connection.execute(
                    text(
                        f"ALTER TABLE human_review_campaigns ADD COLUMN {name} {definition}"
                    )
                )
