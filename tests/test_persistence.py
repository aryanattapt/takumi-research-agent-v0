from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from takumi_research_agent.orchestration.scheduler import enqueue_due_slots, quota_slot_key
from takumi_research_agent.persistence.database import create_database
from takumi_research_agent.persistence.models import Base, BlogProfile, GlobalSettings, ProviderProfile, Run
from takumi_research_agent.persistence.queue import claim_next_run, fenced_stage_update


def test_daily_quota_is_stable_and_scheduler_does_not_duplicate():
    engine, sessions = create_database("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    now = datetime(2026, 11, 6, 1, 30, tzinfo=timezone.utc)  # 08:30 Jakarta
    with sessions.begin() as session:
        session.add(GlobalSettings(id=1, recovery_mode=False, content_mutations_paused=True))
        provider = ProviderProfile(name="Local", provider_type="openai_compatible", base_url="http://localhost:11434/v1", model="test", encrypted_api_key="", capability_result={"status": "reachable"})
        session.add(provider)
        session.flush()
        session.add(BlogProfile(
            name="Science blog", base_url="https://blog.example", provider_id=provider.id,
            active=True, validated_revision=1, capability_result={"list_posts": True, "history_complete": True},
            schedule_enabled=True, schedule_timezone="Asia/Jakarta", publish_time="09:00", lead_minutes=120,
        ))
    with sessions.begin() as session:
        created = enqueue_due_slots(session, now=now)
        assert len(created) == 1
        run_id = created[0].id
    with sessions.begin() as session:
        assert enqueue_due_slots(session, now=now) == []
        run = session.get(Run, run_id)
        assert run.quota_slot_key == quota_slot_key(run.blog_id, date(2026, 11, 6))
        assert run.execution_mode == "dryrun"
    engine.dispose()


def test_expired_worker_cannot_commit_after_lease_epoch_changes():
    engine, sessions = create_database("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with sessions.begin() as session:
        session.add(GlobalSettings(id=1))
        blog = BlogProfile(name="A blog", base_url="https://blog.example")
        session.add(blog)
        session.flush()
        session.add(Run(
            blog_id=blog.id, editorial_date=date(2026, 11, 6), quota_slot_key="slot-key", target_at=datetime.now(timezone.utc),
            status="queued", execution_mode="dryrun", config_snapshot={}, next_run_at=datetime.now(timezone.utc),
        ))
    with sessions.begin() as session:
        old = claim_next_run(session, worker_id="old", lease_seconds=60)
        run_id, epoch, version = old.id, old.lease_epoch, old.state_version
    with sessions.begin() as session:
        run = session.get(Run, run_id)
        run.lease_until = datetime.now(timezone.utc) - timedelta(seconds=1)
    with sessions.begin() as session:
        newer = claim_next_run(session, worker_id="new", lease_seconds=60)
        assert newer.lease_epoch > epoch
    with sessions.begin() as session:
        assert not fenced_stage_update(session, run_id=run_id, worker_id="old", lease_epoch=epoch, expected_version=version, stage="ready", status="running")
        assert fenced_stage_update(session, run_id=run_id, worker_id="new", lease_epoch=newer.lease_epoch, expected_version=newer.state_version, stage="researching", status="running")
    engine.dispose()
