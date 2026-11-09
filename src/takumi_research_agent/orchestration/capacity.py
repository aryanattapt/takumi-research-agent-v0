"""Deadline admission control shared by every blog profile.

The coordinator cannot assume that a single research run is the only work in
flight: at 10 profiles targeting the same 06:00 slot the queue, not one run, is
what decides whether the deadline is reachable. This module packs all pending
runs into the available worker lanes with earliest-deadline-first ordering and
reports honestly when a slot cannot make it (plan §11.4 and §11.5).

Admission control never sends work anywhere. It only rewrites local scheduling
fields (`next_run_at`, `start_by_at`, `capacity_state`) so that the claim query
runs jobs in deadline order and the dashboard can show `at_risk` slots instead
of silently missing the target.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from takumi_research_agent.persistence.models import Run, RunEvent

DEFAULT_CONCURRENCY = 2
DEFAULT_RUN_MINUTES = 45
MIN_RUN_MINUTES = 5
MAX_RUN_MINUTES = 720
MAX_CONCURRENCY = 16

BUSY_STATUSES = ("running", "delivering", "reconciling")

CapacityState = Literal["ok", "at_risk", "infeasible"]


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(int(value), high))


@dataclass(frozen=True, slots=True)
class CapacitySlot:
    """One planned position for a pending run, plus the honest deadline verdict."""

    run_id: str
    blog_id: str
    target_at: datetime
    start_by: datetime
    estimated_start: datetime
    estimated_finish: datetime
    state: CapacityState
    reason: str

    def as_dict(self) -> dict[str, str]:
        return {
            "run_id": self.run_id,
            "blog_id": self.blog_id,
            "target_at": self.target_at.isoformat(),
            "start_by_at": self.start_by.isoformat(),
            "estimated_start_at": self.estimated_start.isoformat(),
            "estimated_finish_at": self.estimated_finish.isoformat(),
            "state": self.state,
            "reason": self.reason,
        }


def plan_capacity(
    runs: Iterable[Run],
    *,
    now: datetime,
    concurrency: int = DEFAULT_CONCURRENCY,
    run_minutes: int = DEFAULT_RUN_MINUTES,
) -> list[CapacitySlot]:
    """Pack queued runs into lanes; a busy lease keeps its lane until it expires."""
    now = _utc(now)
    concurrency = _clamp(concurrency, 1, MAX_CONCURRENCY)
    run_minutes = _clamp(run_minutes, MIN_RUN_MINUTES, MAX_RUN_MINUTES)
    items = list(runs)
    busy = [
        item
        for item in items
        if item.status in BUSY_STATUSES and item.lease_until is not None and _utc(item.lease_until) > now
    ]
    # In-flight work is not free capacity: a run that is still leased cannot be planned over.
    lanes = sorted(_utc(item.lease_until) for item in busy)[:concurrency]
    lanes.extend([now] * (concurrency - len(lanes)))
    pending = [item for item in items if item.status == "queued"]
    pending.sort(key=lambda item: (_utc(item.target_at), _utc(item.created_at or item.target_at), item.id))
    slots: list[CapacitySlot] = []
    for run in pending:
        lane = min(range(concurrency), key=lambda index: lanes[index])
        start = max(now, lanes[lane])
        finish = start + timedelta(minutes=run_minutes)
        lanes[lane] = finish
        target_at = _utc(run.target_at)
        start_by = target_at - timedelta(minutes=run_minutes)
        if now + timedelta(minutes=run_minutes) > target_at:
            state: CapacityState = "infeasible"
            reason = "target_too_close_for_one_run"
        elif start > start_by:
            state = "at_risk"
            reason = "queued_beyond_latest_start"
        else:
            state = "ok"
            reason = "fits_before_target"
        slots.append(
            CapacitySlot(
                run_id=run.id,
                blog_id=run.blog_id,
                target_at=target_at,
                start_by=start_by,
                estimated_start=start,
                estimated_finish=finish,
                state=state,
                reason=reason,
            )
        )
    return slots


def summarize(slots: Iterable[CapacitySlot], *, concurrency: int, run_minutes: int, now: datetime) -> dict[str, Any]:
    slots = list(slots)
    counts = {"ok": 0, "at_risk": 0, "infeasible": 0}
    for slot in slots:
        counts[slot.state] += 1
    return {
        "now": _utc(now).isoformat(),
        "concurrency": concurrency,
        "run_minutes": run_minutes,
        "pending": len(slots),
        "counts": counts,
        "at_risk_run_ids": [slot.run_id for slot in slots if slot.state == "at_risk"],
        "infeasible_run_ids": [slot.run_id for slot in slots if slot.state == "infeasible"],
        "slots": [slot.as_dict() for slot in slots],
    }


def capacity_report(
    session: Session,
    *,
    now: datetime | None = None,
    concurrency: int = DEFAULT_CONCURRENCY,
    run_minutes: int = DEFAULT_RUN_MINUTES,
) -> dict[str, Any]:
    """Read-only plan preview for the dashboard and API; never mutates a run."""
    now = _utc(now or datetime.now(timezone.utc))
    concurrency = _clamp(concurrency, 1, MAX_CONCURRENCY)
    run_minutes = _clamp(run_minutes, MIN_RUN_MINUTES, MAX_RUN_MINUTES)
    runs = session.scalars(
        select(Run).where(Run.status.in_(("queued",) + BUSY_STATUSES)).order_by(Run.target_at, Run.created_at)
    ).all()
    slots = plan_capacity(runs, now=now, concurrency=concurrency, run_minutes=run_minutes)
    return summarize(slots, concurrency=concurrency, run_minutes=run_minutes, now=now)


def apply_admission_control(
    session: Session,
    *,
    now: datetime | None = None,
    concurrency: int = DEFAULT_CONCURRENCY,
    run_minutes: int = DEFAULT_RUN_MINUTES,
) -> dict[str, Any]:
    """Re-plan every queued run and persist deadline-aware scheduling fields."""
    now = _utc(now or datetime.now(timezone.utc))
    concurrency = _clamp(concurrency, 1, MAX_CONCURRENCY)
    run_minutes = _clamp(run_minutes, MIN_RUN_MINUTES, MAX_RUN_MINUTES)
    runs = session.scalars(
        select(Run).where(Run.status.in_(("queued",) + BUSY_STATUSES)).order_by(Run.target_at, Run.created_at)
    ).all()
    slots = plan_capacity(runs, now=now, concurrency=concurrency, run_minutes=run_minutes)
    by_id = {run.id: run for run in runs}
    for slot in slots:
        run = by_id[slot.run_id]
        if run.capacity_state != slot.state:
            session.add(
                RunEvent(
                    run_id=run.id,
                    event_type=f"capacity_{slot.state}",
                    details={
                        "reason": slot.reason,
                        "target_at": slot.target_at.isoformat(),
                        "start_by_at": slot.start_by.isoformat(),
                        "estimated_start_at": slot.estimated_start.isoformat(),
                        "concurrency": concurrency,
                        "run_minutes": run_minutes,
                    },
                )
            )
        run.capacity_state = slot.state
        run.start_by_at = slot.start_by
        # The claim query orders by next_run_at, so EDF ordering has to live there.
        run.next_run_at = max(now, slot.estimated_start)
    session.flush()
    return summarize(slots, concurrency=concurrency, run_minutes=run_minutes, now=now)
