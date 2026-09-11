"""add file objects

Revision ID: a7d3c98e61f4
Revises: 38489f87dcae
Create Date: 2026-09-11 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a7d3c98e61f4"
down_revision: str | None = "38489f87dcae"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "file_objects",
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column("category", sa.String(length=32), nullable=False),
        sa.Column("original_name", sa.String(length=255), nullable=False),
        sa.Column("storage_key", sa.String(length=512), nullable=False),
        sa.Column("content_type", sa.String(length=255), nullable=True),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"]),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("storage_key"),
    )
    op.create_index(op.f("ix_file_objects_category"), "file_objects", ["category"], unique=False)
    op.create_index(
        op.f("ix_file_objects_created_by"), "file_objects", ["created_by"], unique=False
    )
    op.create_index(
        op.f("ix_file_objects_project_id"), "file_objects", ["project_id"], unique=False
    )
    op.create_index(op.f("ix_file_objects_sha256"), "file_objects", ["sha256"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_file_objects_sha256"), table_name="file_objects")
    op.drop_index(op.f("ix_file_objects_project_id"), table_name="file_objects")
    op.drop_index(op.f("ix_file_objects_created_by"), table_name="file_objects")
    op.drop_index(op.f("ix_file_objects_category"), table_name="file_objects")
    op.drop_table("file_objects")
