"""add semantic vectors for synchronized blog history

Revision ID: 8e5a410c6d22
Revises: 7d36b80a2a11
Create Date: 2026-11-10 20:00:00.000000
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "8e5a410c6d22"
down_revision: Union[str, None] = "7d36b80a2a11"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "blog_history_embeddings",
        sa.Column("blog_id", sa.String(length=36), nullable=False),
        sa.Column("embedding_model", sa.String(length=200), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("vector", sa.JSON(), nullable=False),
        sa.Column("dimensions", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["blog_id"], ["blog_profiles.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("blog_id", "embedding_model", "content_hash"),
    )


def downgrade() -> None:
    op.drop_table("blog_history_embeddings")
