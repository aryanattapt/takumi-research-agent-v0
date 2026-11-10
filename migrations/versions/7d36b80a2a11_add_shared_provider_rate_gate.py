"""add shared provider request gate

Revision ID: 7d36b80a2a11
Revises: c90a12b7e6d4
Create Date: 2026-11-10 19:30:00.000000
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "7d36b80a2a11"
down_revision: Union[str, None] = "c90a12b7e6d4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "provider_rate_gates",
        sa.Column("key_hash", sa.String(length=64), primary_key=True),
        sa.Column("next_allowed_at", sa.Float(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_table("provider_rate_gates")
