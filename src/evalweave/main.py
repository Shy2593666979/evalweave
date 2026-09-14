from __future__ import annotations

import argparse
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from evalweave.api.router import api_router
from evalweave.auth.service import bootstrap_identity_data
from evalweave.core.config import get_settings, set_config_path
from evalweave.core.logging import configure_logging
from evalweave.db.session import create_db_and_tables
from evalweave.workspace import ensure_single_workspace


@asynccontextmanager
async def lifespan(_: FastAPI):
    settings = get_settings()
    configure_logging(settings)
    settings.storage.local_directory.mkdir(parents=True, exist_ok=True)
    settings.logging.directory.mkdir(parents=True, exist_ok=True)
    create_db_and_tables()
    ensure_single_workspace()
    bootstrap_identity_data()
    yield


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title=settings.application.name,
        version=settings.application.version,
        debug=settings.application.debug,
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(api_router, prefix="/api")
    return app


def api_main() -> None:
    parser = argparse.ArgumentParser(description="Run the EvalWeave API")
    parser.add_argument("--config", type=Path, default=Path("config/application.yaml"))
    args = parser.parse_args()
    set_config_path(args.config)
    settings = get_settings()
    uvicorn.run(
        create_app(),
        host=settings.server.host,
        port=settings.server.port,
        reload=False,
    )
