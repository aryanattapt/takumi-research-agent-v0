from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, or_, select, update
from sqlalchemy.orm import Session

from .models import Run


def claim_next_run(session: Session, *, worker_id: str, lease_seconds: int = 90) -> Run | None:
    """Claim one queued/expired job in a short transaction; never hold it over network I/O."""
    now = datetime.now(timezone.utc)
    query = (
        select(Run)
        .where(
            Run.next_run_at <= now,
            or_(Run.status.in_(("queued", "retry_wait")), and_(Run.status.in_(("running", "delivering", "reconciling")), or_(Run.lease_until.is_(None), Run.lease_until < now))),
        )
        .order_by(Run.next_run_at, Run.created_at)
        .with_for_update(skip_locked=True)
        .limit(1)
    )
    run = session.scalar(query)
    if run is None:
        return None
    run.status = "running"
    run.lease_owner = worker_id
    run.lease_until = now + timedelta(seconds=lease_seconds)
    run.lease_epoch += 1
    run.attempts += 1
    run.state_version += 1
    session.flush()
    return run


def fenced_stage_update(session: Session, *, run_id: str, worker_id: str, lease_epoch: int, expected_version: int, stage: str, status: str) -> bool:
    now = datetime.now(timezone.utc)
    result = session.execute(
        update(Run)
        .where(
            Run.id == run_id,
            Run.lease_owner == worker_id,
            Run.lease_epoch == lease_epoch,
            Run.state_version == expected_version,
            Run.lease_until >= now,
        )
        .values(stage=stage, status=status, state_version=Run.state_version + 1, updated_at=now)
    )
    return result.rowcount == 1


def renew_lease(session: Session, *, run_id: str, worker_id: str, lease_epoch: int, lease_seconds: int = 90) -> bool:
    now = datetime.now(timezone.utc)
    result = session.execute(
        update(Run)
        .where(Run.id == run_id, Run.lease_owner == worker_id, Run.lease_epoch == lease_epoch, Run.lease_until >= now)
        .values(lease_until=now + timedelta(seconds=lease_seconds), updated_at=now)
    )
    return result.rowcount == 1
