from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class FileCategory(StrEnum):
    DATASET_SOURCE = "dataset_source"
    GENERATED_SCRIPT = "generated_script"
    EXECUTION_LOG = "execution_log"
    CONVERSATION_TRACE = "conversation_trace"
    EVALUATION_RESULT = "evaluation_result"
    REPORT = "report"
    SKILL_PACKAGE = "skill_package"


class FileRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    project_id: UUID
    created_by: UUID
    category: str
    original_name: str
    content_type: str | None
    size_bytes: int
    sha256: str
    created_at: datetime
