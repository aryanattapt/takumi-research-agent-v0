"""add deadline admission control fields to runs

Revision ID: 3c9d1f7ab204
Revises: f8fc896d9613
Create Date: 2026-11-06 14:20:00.000000
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '3c9d1f7ab204'
down_revision: Union[str, None] = 'f8fc896d9613'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

CHECK_NAME = 'ck_run_capacity_state'
CHECK_CONDITION = "capacity_state in ('ok', 'at_risk', 'infeasible')"


def upgrade() -> None:
    op.add_column('runs', sa.Column('schedule_version', sa.Integer(), nullable=False, server_default='1'))
    op.add_column('runs', sa.Column('capacity_state', sa.String(length=20), nullable=False, server_default='ok'))
    op.add_column('runs', sa.Column('start_by_at', sa.DateTime(timezone=True), nullable=True))
    # SQLite cannot ALTER TABLE ADD CONSTRAINT, and a batch table rebuild on the runs
    # table (FKs + named indexes) is riskier than the guard itself; the model still
    # declares the constraint for fresh databases.
    if op.get_bind().dialect.name == 'postgresql':
        op.create_check_constraint(CHECK_NAME, 'runs', CHECK_CONDITION)


def downgrade() -> None:
    if op.get_bind().dialect.name == 'postgresql':
        op.drop_constraint(CHECK_NAME, 'runs', type_='check')
    op.drop_column('runs', 'start_by_at')
    op.drop_column('runs', 'capacity_state')
    op.drop_column('runs', 'schedule_version')
