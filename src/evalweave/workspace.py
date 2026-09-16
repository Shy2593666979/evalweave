from __future__ import annotations

from sqlmodel import Session, select

from evalweave.db.models import Project, ProjectMember, SystemRole, User
from evalweave.db.session import get_engine

DEFAULT_WORKSPACE_NAME = "EvalWeave 工作空间"
DEFAULT_WORKSPACE_DESCRIPTION = "平台统一的数据与评测工作空间"

def ensure_single_workspace(session: Session | None = None) -> Project:
    """Keep a default project for new and upgraded installations."""
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

        return projects[0]
    finally:
        if owns_session:
            active_session.close()


def bootstrap_legacy_project_memberships() -> None:
    """Give existing regular users access to the legacy default project once."""
    with Session(get_engine()) as session:
        if session.exec(select(ProjectMember)).first() is not None:
            return
        workspace = ensure_single_workspace(session)
        users = session.exec(select(User).where(User.system_role == SystemRole.USER)).all()
        for user in users:
            session.add(ProjectMember(project_id=workspace.id, user_id=user.id))
        session.commit()
