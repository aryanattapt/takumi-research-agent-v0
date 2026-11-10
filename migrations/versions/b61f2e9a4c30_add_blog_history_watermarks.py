"""add delta watermark to blog history sync state

Revision ID: b61f2e9a4c30
Revises: a48c2d7f6e10
Create Date: 2026-11-10 11:00:00.000000
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "b61f2e9a4c30"
down_revision: Union[str, None] = "a48c2d7f6e10"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "blog_history_sync_states",
        sa.Column("sync_mode", sa.String(length=20), nullable=False, server_default="full"),
    )
    op.add_column("blog_history_sync_states", sa.Column("sync_since", sa.DateTime(timezone=True), nullable=True))
    op.add_column("blog_history_sync_states", sa.Column("committed_watermark", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("blog_history_sync_states", "committed_watermark")
    op.drop_column("blog_history_sync_states", "sync_since")
    op.drop_column("blog_history_sync_states", "sync_mode")
