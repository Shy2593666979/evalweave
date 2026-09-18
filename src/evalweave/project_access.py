"""Backward-compatible project access imports.

New code should import these helpers from ``evalweave.services.projects``.
"""

from evalweave.services.projects import ProjectService

project_ids_for_user = ProjectService.accessible_ids
require_project_access = ProjectService.require_access

__all__ = ["project_ids_for_user", "require_project_access"]
