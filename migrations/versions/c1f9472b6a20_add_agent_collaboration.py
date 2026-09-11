"""add agent collaboration

Revision ID: c1f9472b6a20
Revises: a7d3c98e61f4
Create Date: 2026-09-11 00:10:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c1f9472b6a20"
down_revision: str | None = "a7d3c98e61f4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "agent_jobs",
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column("source_file_id", sa.Uuid(), nullable=True),
        sa.Column("result_file_id", sa.Uuid(), nullable=True),
        sa.Column("title", sa.String(length=128), nullable=False),
        sa.Column("goal", sa.Text(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "PENDING",
                "DISCOVERING",
                "PLANNING",
                "WAITING_HUMAN",
                "RUNNING",
                "ANALYZING",
                "COMPLETED",
                "FAILED",
                "CANCELLED",
                name="agentjobstatus",
            ),
            nullable=False,
        ),
        sa.Column("input_config", sa.JSON(), nullable=False),
        sa.Column("eval_spec", sa.JSON(), nullable=False),
        sa.Column("result", sa.JSON(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("repair_attempts", sa.Integer(), nullable=False),
        sa.Column("max_repair_attempts", sa.Integer(), nullable=False),
        sa.Column("requires_approval", sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"]),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"]),
        sa.ForeignKeyConstraint(["result_file_id"], ["file_objects.id"]),
        sa.ForeignKeyConstraint(["source_file_id"], ["file_objects.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_agent_jobs_created_by"), "agent_jobs", ["created_by"], unique=False)
    op.create_index(op.f("ix_agent_jobs_project_id"), "agent_jobs", ["project_id"], unique=False)
    op.create_index(
        op.f("ix_agent_jobs_result_file_id"), "agent_jobs", ["result_file_id"], unique=False
    )
    op.create_index(
        op.f("ix_agent_jobs_source_file_id"), "agent_jobs", ["source_file_id"], unique=False
    )
    op.create_index(op.f("ix_agent_jobs_status"), "agent_jobs", ["status"], unique=False)

    op.create_table(
        "agent_steps",
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column(
            "status",
            sa.Enum("PENDING", "RUNNING", "COMPLETED", "FAILED", name="stepstatus"),
            nullable=False,
        ),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("input_data", sa.JSON(), nullable=False),
        sa.Column("output_data", sa.JSON(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["job_id"], ["agent_jobs.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_agent_steps_job_id"), "agent_steps", ["job_id"], unique=False)
    op.create_index(op.f("ix_agent_steps_name"), "agent_steps", ["name"], unique=False)
    op.create_index(op.f("ix_agent_steps_status"), "agent_steps", ["status"], unique=False)

    op.create_table(
        "human_tasks",
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.String(length=128), nullable=False),
        sa.Column("instructions", sa.Text(), nullable=False),
        sa.Column(
            "status",
            sa.Enum("PENDING", "APPROVED", "REJECTED", "CANCELLED", name="humantaskstatus"),
            nullable=False,
        ),
        sa.Column("notification_targets", sa.JSON(), nullable=False),
        sa.Column("decision_reason", sa.Text(), nullable=True),
        sa.Column("resolved_by", sa.Uuid(), nullable=True),
        sa.Column("resolved_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["job_id"], ["agent_jobs.id"]),
        sa.ForeignKeyConstraint(["resolved_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_human_tasks_job_id"), "human_tasks", ["job_id"], unique=False)
    op.create_index(
        op.f("ix_human_tasks_resolved_by"), "human_tasks", ["resolved_by"], unique=False
    )
    op.create_index(op.f("ix_human_tasks_status"), "human_tasks", ["status"], unique=False)

    op.create_table(
        "notification_deliveries",
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("human_task_id", sa.Uuid(), nullable=False),
        sa.Column("channel", sa.String(length=32), nullable=False),
        sa.Column("recipient", sa.String(length=255), nullable=False),
        sa.Column(
            "status",
            sa.Enum("PENDING", "SENT", "FAILED", name="deliverystatus"),
            nullable=False,
        ),
        sa.Column("provider_message_id", sa.String(length=255), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("sent_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["human_task_id"], ["human_tasks.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_notification_deliveries_channel"),
        "notification_deliveries",
        ["channel"],
        unique=False,
    )
    op.create_index(
        op.f("ix_notification_deliveries_human_task_id"),
        "notification_deliveries",
        ["human_task_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_notification_deliveries_status"),
        "notification_deliveries",
        ["status"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_notification_deliveries_status"), table_name="notification_deliveries"
    )
    op.drop_index(
        op.f("ix_notification_deliveries_human_task_id"),
        table_name="notification_deliveries",
    )
    op.drop_index(
        op.f("ix_notification_deliveries_channel"), table_name="notification_deliveries"
    )
    op.drop_table("notification_deliveries")
    op.drop_index(op.f("ix_human_tasks_status"), table_name="human_tasks")
    op.drop_index(op.f("ix_human_tasks_resolved_by"), table_name="human_tasks")
    op.drop_index(op.f("ix_human_tasks_job_id"), table_name="human_tasks")
    op.drop_table("human_tasks")
    op.drop_index(op.f("ix_agent_steps_status"), table_name="agent_steps")
    op.drop_index(op.f("ix_agent_steps_name"), table_name="agent_steps")
    op.drop_index(op.f("ix_agent_steps_job_id"), table_name="agent_steps")
    op.drop_table("agent_steps")
    op.drop_index(op.f("ix_agent_jobs_status"), table_name="agent_jobs")
    op.drop_index(op.f("ix_agent_jobs_source_file_id"), table_name="agent_jobs")
    op.drop_index(op.f("ix_agent_jobs_result_file_id"), table_name="agent_jobs")
    op.drop_index(op.f("ix_agent_jobs_project_id"), table_name="agent_jobs")
    op.drop_index(op.f("ix_agent_jobs_created_by"), table_name="agent_jobs")
    op.drop_table("agent_jobs")
