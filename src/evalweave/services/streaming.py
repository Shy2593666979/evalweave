from __future__ import annotations

from uuid import UUID

from sqlalchemy.engine import Engine
from sqlmodel import Session, select

from evalweave.db.models import (
    AgentJob,
    AgentJobEvent,
    AgentStep,
    AssistantConversation,
    AssistantMessage,
)


class StreamingService:
    """Read short-lived snapshots for long-running HTTP streams."""

    @staticmethod
    def assistant_messages(
        engine: Engine,
        conversation_id: UUID,
        owner_id: UUID,
    ) -> list[AssistantMessage] | None:
        with Session(engine) as session:
            conversation = session.get(AssistantConversation, conversation_id)
            if conversation is None or conversation.created_by != owner_id:
                return None
            statement = (
                select(AssistantMessage)
                .where(AssistantMessage.conversation_id == conversation_id)
                .order_by(AssistantMessage.created_at)
            )
            return list(session.exec(statement).all())

    @staticmethod
    def conversation_title(
        engine: Engine,
        conversation_id: UUID,
        owner_id: UUID,
    ) -> AssistantConversation | None:
        with Session(engine) as session:
            conversation = session.get(AssistantConversation, conversation_id)
            if conversation is None or conversation.created_by != owner_id:
                return None
            session.expunge(conversation)
            return conversation

    @staticmethod
    def job_snapshot(
        engine: Engine,
        job_id: UUID,
        after_event_id: int,
    ) -> tuple[list[AgentJobEvent], AgentJob | None, list[AgentStep]]:
        with Session(engine) as session:
            events = list(
                session.exec(
                    select(AgentJobEvent)
                    .where(
                        AgentJobEvent.job_id == job_id,
                        AgentJobEvent.id > after_event_id,
                    )
                    .order_by(AgentJobEvent.id)
                ).all()
            )
            job = session.get(AgentJob, job_id)
            steps = list(
                session.exec(
                    select(AgentStep)
                    .where(AgentStep.job_id == job_id)
                    .order_by(AgentStep.created_at)
                ).all()
            )
            for item in [*events, *steps]:
                session.expunge(item)
            if job is not None:
                session.expunge(job)
            return events, job, steps
