from __future__ import annotations

import asyncio
import hashlib
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from takumi_research_agent.persistence.models import BlogProfile, GlobalSettings, ProviderProfile, Run


def quota_slot_key(blog_id: str, editorial_date: date, ordinal: int = 1) -> str:
    value = f"{blog_id}\0{editorial_date.isoformat()}\0{ordinal}".encode("utf-8")
    return hashlib.sha256(value).hexdigest()


def _snapshot(blog: BlogProfile, provider: ProviderProfile | None) -> dict:
    return {
        "blog_id": blog.id,
        "blog_name": blog.name,
        "blog_revision": blog.revision,
        "destination_epoch": blog.destination_epoch,
        "base_url": blog.base_url,
        "provider_id": provider.id if provider else None,
        "provider_revision": provider.revision if provider else None,
        "provider_type": provider.provider_type if provider else None,
        "provider_base_url": provider.base_url if provider else None,
        "provider_model": provider.model if provider else None,
        "adapter_mapping": blog.adapter_mapping,
        "capabilities": blog.capabilities,
        "topic_settings": blog.topic_settings,
        "editorial_settings": blog.editorial_settings,
        "schedule_timezone": blog.schedule_timezone,
        "publish_time": blog.publish_time,
        "lead_minutes": blog.lead_minutes,
    }


def enqueue_due_slots(session: Session, *, now: datetime | None = None) -> list[Run]:
    """Create one stable daily run per active profile; missed slots are recorded, never caught up."""
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    created: list[Run] = []
    profiles = session.scalars(
        select(BlogProfile).where(BlogProfile.active.is_(True), BlogProfile.schedule_enabled.is_(True)).order_by(BlogProfile.id)
    ).all()
    control = session.get(GlobalSettings, 1)
    if control is None or control.recovery_mode:
        return created
    for blog in profiles:
        provider = session.get(ProviderProfile, blog.provider_id) if blog.provider_id else None
        if (
            blog.validated_revision != blog.revision
            or not blog.capability_result.get("history_complete")
            or provider is None
            or not provider.active
            or provider.capability_result.get("status") != "reachable"
        ):
            continue
        try:
            tz = ZoneInfo(blog.schedule_timezone)
            hour, minute = (int(item) for item in blog.publish_time.split(":", 1))
        except (ZoneInfoNotFoundError, ValueError):
            continue
        local_now = now.astimezone(tz)
        editorial_date = local_now.date()
        target_local = datetime.combine(editorial_date, time(hour, minute), tzinfo=tz)
        target_at = target_local.astimezone(timezone.utc)
        prepare_at = target_at - timedelta(minutes=blog.lead_minutes)
        if now < prepare_at:
            continue
        key = quota_slot_key(blog.id, editorial_date)
        existing = session.scalar(select(Run).where(Run.quota_slot_key == key))
        if existing:
            continue
        missed = now >= target_at
        run = Run(
            blog_id=blog.id,
            editorial_date=editorial_date,
            quota_ordinal=1,
            quota_slot_key=key,
            target_at=target_at,
            status="completed" if missed else "queued",
            stage="deadline_missed" if missed else "queued",
            # Until an external write capability is proven, the scheduled worker only stores local drafts.
            execution_mode="dryrun",
            config_snapshot=_snapshot(blog, provider),
            pipeline_outcome="missed_deadline" if missed else "queued",
            deadline_result="missed" if missed else "pending",
            next_run_at=now,
        )
        try:
            with session.begin_nested():
                session.add(run)
                session.flush()
            created.append(run)
        except IntegrityError:
            # A concurrent scheduler won the unique quota slot; do not retry as another slot.
            continue
    return created


class PipelineProcessor:
    async def process(self, run: Run) -> dict:
        """Production pipeline is injected after provider and blog capability gates pass."""
        raise NotImplementedError("No research pipeline processor has been configured")


class RunWorker:
    def __init__(self, sessions, *, worker_id: str, processor: PipelineProcessor, concurrency: int = 2):
        self.sessions = sessions
        self.worker_id = worker_id
        self.processor = processor
        self.concurrency = max(1, min(concurrency, 4))

    async def run_once(self) -> bool:
        with self.sessions.begin() as session:
            from takumi_research_agent.persistence.queue import claim_next_run

            run = claim_next_run(session, worker_id=self.worker_id)
            if run is None:
                return False
            run_id = run.id
            epoch = run.lease_epoch
        heartbeat_stop = asyncio.Event()
        heartbeat_task = asyncio.create_task(self._heartbeat(run_id, epoch, heartbeat_stop))
        outcome: dict = {}
        try:
            outcome = await self.processor.process(run)
            status = outcome.get("status", "blocked")
            reason = outcome.get("reason", "")
        except Exception as exc:  # the durable run record exposes a sanitized category, never exception text with headers.
            status = "blocked"
            reason = getattr(exc, "code", type(exc).__name__)
        finally:
            heartbeat_stop.set()
            heartbeat_task.cancel()
            try:
                await heartbeat_task
            except asyncio.CancelledError:
                pass
            except Exception:
                # A failed heartbeat is fenced at final commit; the run will be reclaimed if the lease expired.
                pass
        with self.sessions.begin() as session:
            from takumi_research_agent.persistence.queue import fenced_stage_update

            expected_version = run.state_version
            accepted = fenced_stage_update(
                session,
                run_id=run_id,
                worker_id=self.worker_id,
                lease_epoch=epoch,
                expected_version=expected_version,
                stage="completed" if status == "completed" else "blocked",
                status="completed" if status == "completed" else "blocked",
            )
            if accepted:
                completed_run = session.get(Run, run_id)
                if reason:
                    completed_run.pipeline_outcome = reason[:80]
                if outcome.get("editorial_decision") in {"publish", "draft", "skip", "blocked"}:
                    completed_run.editorial_decision = outcome["editorial_decision"]
                if outcome.get("pair_completeness") in {"none", "partial", "complete"}:
                    completed_run.pair_completeness = outcome["pair_completeness"]
                if outcome.get("deadline_result") in {"pending", "met", "late", "missed", "blocked"}:
                    completed_run.deadline_result = outcome["deadline_result"]
        return True

    async def _heartbeat(self, run_id: str, lease_epoch: int, stop_event) -> None:
        import asyncio

        while not stop_event.is_set():
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=25)
                return
            except TimeoutError:
                pass
            with self.sessions.begin() as session:
                from takumi_research_agent.persistence.queue import renew_lease

                if not renew_lease(session, run_id=run_id, worker_id=self.worker_id, lease_epoch=lease_epoch):
                    return
