from __future__ import annotations

import re
from typing import Any
from uuid import UUID

from sqlmodel import Session

from evalweave.db.models import AgentJobEvent
from evalweave.db.session import get_engine


_SENSITIVE_VALUE_PATTERNS = (
    (
        re.compile(r"(?i)(authorization\s*[:=]\s*[\"']?bearer\s+)[^\s\"',}]+"),
        r"\1[REDACTED]",
    ),
    (
        re.compile(
            r"(?i)([\"']?(?:api[_-]?key|access[_-]?token|refresh[_-]?token|password|secret)"
            r"[\"']?\s*[:=]\s*[\"'])[^\"']+([\"'])"
        ),
        r"\1[REDACTED]\2",
    ),
)


def redact_model_output(content: str) -> str:
    """Remove common credential values before an event is persisted or streamed."""
    redacted = content
    for pattern, replacement in _SENSITIVE_VALUE_PATTERNS:
        redacted = pattern.sub(replacement, redacted)
    return redacted


def emit_job_event(
    job_id: UUID,
    phase: str,
    event_type: str,
    content: str = "",
    payload: dict[str, Any] | None = None,
) -> None:
    with Session(get_engine()) as session:
        session.add(
            AgentJobEvent(
                job_id=job_id,
                phase=phase,
                event_type=event_type,
                content=content,
                payload=payload or {},
            )
        )
        session.commit()


class ModelEventPublisher:
    def __init__(self, job_id: UUID, phase: str, label: str) -> None:
        self.job_id = job_id
        self.phase = phase
        self.buffer = ""
        emit_job_event(job_id, phase, "model_start", payload={"label": label})

    def write(self, delta: str) -> None:
        self.buffer += delta
        if len(self.buffer) >= 32 or "\n" in self.buffer:
            self.flush()

    def flush(self) -> None:
        if not self.buffer:
            return
        emit_job_event(
            self.job_id,
            self.phase,
            "model_delta",
            redact_model_output(self.buffer),
        )
        self.buffer = ""

    def complete(self) -> None:
        self.flush()
        emit_job_event(self.job_id, self.phase, "model_complete")

    def fail(self, error: Exception) -> None:
        self.flush()
        emit_job_event(
            self.job_id,
            self.phase,
            "model_error",
            redact_model_output(str(error))[:2000],
        )
