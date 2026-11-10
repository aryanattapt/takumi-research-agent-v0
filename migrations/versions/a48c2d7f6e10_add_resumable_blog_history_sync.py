"""add resumable blog history sync state

Revision ID: a48c2d7f6e10
Revises: 5b7e2c41d9a0
Create Date: 2026-11-10 10:20:00.000000
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a48c2d7f6e10"
down_revision: Union[str, None] = "5b7e2c41d9a0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "blog_history_sync_states",
        sa.Column("blog_id", sa.String(length=36), nullable=False),
        sa.Column("sync_run_id", sa.String(length=36), nullable=False),
        sa.Column("mapping_hash", sa.String(length=64), nullable=False),
        sa.Column("snapshot_id", sa.String(length=36), nullable=False),
        sa.Column("checkpoint", sa.JSON(), nullable=True),
        sa.Column("complete", sa.Boolean(), nullable=False),
        sa.Column("records_synced", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
        sa.ForeignKeyConstraint(["blog_id"], ["blog_profiles.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("blog_id"),
    )


def downgrade() -> None:
    op.drop_table("blog_history_sync_states")
