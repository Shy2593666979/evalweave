from __future__ import annotations

from sqlmodel import Session, select

from evalweave.db.models import (
    AgentJob,
    AssistantConversation,
    Dataset,
    Experiment,
    FileObject,
    Project,
)
from evalweave.db.session import get_engine

DEFAULT_WORKSPACE_NAME = "EvalWeave 工作空间"
DEFAULT_WORKSPACE_DESCRIPTION = "平台统一的数据与评测工作空间"

PROJECT_SCOPED_MODELS = (
    FileObject,
    AgentJob,
    AssistantConversation,
    Dataset,
    Experiment,
)


def ensure_single_workspace(session: Session | None = None) -> Project:
    """Return the one internal workspace and merge legacy project partitions into it."""
    owns_session = session is None
    active_session = session or Session(get_engine())
    try:
        projects = list(
            active_session.exec(select(Project).order_by(Project.created_at)).all()
        )
        if not projects:
            workspace = Project(
                name=DEFAULT_WORKSPACE_NAME,
                description=DEFAULT_WORKSPACE_DESCRIPTION,
            )
            active_session.add(workspace)
            active_session.commit()
            active_session.refresh(workspace)
            return workspace

        workspace = projects[0]
        legacy_ids = {project.id for project in projects[1:]}
        if legacy_ids:
            for model in PROJECT_SCOPED_MODELS:
                records = active_session.exec(
                    select(model).where(model.project_id.in_(legacy_ids))
                ).all()
                for record in records:
                    record.project_id = workspace.id
                    active_session.add(record)
            for project in projects[1:]:
                active_session.delete(project)
            active_session.commit()
            active_session.refresh(workspace)
        return workspace
    finally:
        if owns_session:
            active_session.close()
