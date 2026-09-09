from collections.abc import Generator
from functools import lru_cache
from pathlib import Path

from sqlalchemy.engine import Engine
from sqlalchemy.engine.url import make_url
from sqlmodel import Session, create_engine

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
