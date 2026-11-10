"""add per-profile RSS feeds

Revision ID: c90a12b7e6d4
Revises: b61f2e9a4c30
Create Date: 2026-11-10 17:20:00.000000
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c90a12b7e6d4"
down_revision: Union[str, None] = "b61f2e9a4c30"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("blog_profiles", sa.Column("rss_feeds", sa.JSON(), nullable=False, server_default=sa.text("'[]'")))


def downgrade() -> None:
    op.drop_column("blog_profiles", "rss_feeds")
