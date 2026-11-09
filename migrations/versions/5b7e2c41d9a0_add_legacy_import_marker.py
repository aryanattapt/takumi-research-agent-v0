"""add legacy import marker table

Revision ID: 5b7e2c41d9a0
Revises: 3c9d1f7ab204
Create Date: 2026-11-06 15:10:00.000000
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '5b7e2c41d9a0'
down_revision: Union[str, None] = '3c9d1f7ab204'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'legacy_import_records',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('source_fingerprint', sa.String(length=64), nullable=False),
        sa.Column('source_key', sa.String(length=200), nullable=False),
        sa.Column('table_name', sa.String(length=40), nullable=False),
        sa.Column('record_key', sa.String(length=400), nullable=False),
        sa.Column('target_type', sa.String(length=60), nullable=False),
        sa.Column('target_id', sa.String(length=200), nullable=False),
        sa.Column('imported_at', sa.DateTime(timezone=True), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('source_fingerprint', 'table_name', 'record_key', name='uq_legacy_import_record'),
    )


def downgrade() -> None:
    op.drop_table('legacy_import_records')
