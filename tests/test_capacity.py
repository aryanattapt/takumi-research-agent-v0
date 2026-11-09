from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from takumi_research_agent.orchestration.capacity import apply_admission_control, capacity_report, plan_capacity
from takumi_research_agent.orchestration.scheduler import enqueue_due_slots
from takumi_research_agent.persistence.database import create_database
from takumi_research_agent.persistence.models import Base, BlogProfile, GlobalSettings, ProviderProfile, Run, RunEvent

NOW = datetime(2026, 11, 6, 1, 0, tzinfo=timezone.utc)


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _bootstrap(session, count: int = 1, **blog_kwargs) -> list[BlogProfile]:
    session.add(GlobalSettings(id=1, recovery_mode=False, content_mutations_paused=True))
    provider = ProviderProfile(
        name="Local model",
        provider_type="openai_compatible",
        base_url="http://localhost:11434/v1",
        model="test",
        encrypted_api_key="",
        capability_result={"status": "reachable"},
    )
    session.add(provider)
    session.flush()
    blogs = []
    for index in range(count):
        blog = BlogProfile(
            name=f"Blog {index}",
            base_url=f"https://blog-{index}.example/api",
            provider_id=provider.id,
            **blog_kwargs,
        )
        session.add(blog)
        blogs.append(blog)
    session.flush()
    return blogs


def _run(
    blog: BlogProfile,
    target_at: datetime,
    *,
    ordinal: int = 1,
    status: str = "queued",
    created_at: datetime | None = None,
    lease_until: datetime | None = None,
) -> Run:
    key = hashlib.sha256(f"{blog.id}:{target_at.isoformat()}:{ordinal}".encode()).hexdigest()
    return Run(
        blog_id=blog.id,
        editorial_date=target_at.date(),
        quota_ordinal=ordinal,
        quota_slot_key=key,
        target_at=target_at,
        status=status,
        stage="queued",
        execution_mode="dryrun",
        config_snapshot={},
        next_run_at=created_at or NOW,
        lease_until=lease_until,
    )


def _capacity_events(session) -> list[RunEvent]:
    return list(session.scalars(select(RunEvent).where(RunEvent.event_type.like("capacity_%"))))


def test_capacity_packs_one_lane_in_deadline_order_and_flags_at_risk():
    engine, sessions = create_database("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    target = NOW + timedelta(hours=1)
    with sessions.begin() as session:
        [blog] = _bootstrap(session)
        session.add_all([_run(blog, target, ordinal=index) for index in (1, 2, 3)])
    with sessions.begin() as session:
        runs = session.scalars(select(Run).order_by(Run.quota_ordinal)).all()
        slots = plan_capacity(runs, now=NOW, concurrency=1, run_minutes=45)
        assert [slot.state for slot in slots] == ["ok", "at_risk", "at_risk"]
        assert slots[0].start_by == target - timedelta(minutes=45)
        assert slots[0].reason == "fits_before_target"
        assert slots[1].reason == "queued_beyond_latest_start"
        assert [slot.estimated_start for slot in slots] == [
            NOW,
            NOW + timedelta(minutes=45),
            NOW + timedelta(minutes=90),
        ]
    engine.dispose()


def test_capacity_marks_infeasible_when_target_is_inside_one_run_window():
    engine, sessions = create_database("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with sessions.begin() as session:
        [blog] = _bootstrap(session)
        session.add(_run(blog, NOW + timedelta(minutes=30)))
    with sessions.begin() as session:
        runs = session.scalars(select(Run)).all()
        slots = plan_capacity(runs, now=NOW, concurrency=4, run_minutes=45)
        assert [slot.state for slot in slots] == ["infeasible"]
        assert slots[0].reason == "target_too_close_for_one_run"
    engine.dispose()


def test_busy_lease_holds_a_lane_and_completed_runs_are_ignored():
    engine, sessions = create_database("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    target = NOW + timedelta(hours=2)
    with sessions.begin() as session:
        [blog] = _bootstrap(session)
        session.add_all(
            [
                _run(blog, target, ordinal=1, status="running", lease_until=NOW + timedelta(minutes=50)),
                _run(blog, target, ordinal=2),
                _run(blog, target, ordinal=3, status="completed"),
            ]
        )
    with sessions.begin() as session:
        runs = session.scalars(select(Run).order_by(Run.quota_ordinal)).all()
        slots = plan_capacity(runs, now=NOW, concurrency=1, run_minutes=45)
        assert [slot.run_id for slot in slots] == [runs[1].id]
        assert slots[0].estimated_start == NOW + timedelta(minutes=50)
    engine.dispose()


def test_admission_control_persists_the_plan_and_records_each_state_change_once():
    engine, sessions = create_database("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    target = NOW + timedelta(hours=1)
    with sessions.begin() as session:
        [blog] = _bootstrap(session)
        session.add_all([_run(blog, target, ordinal=index) for index in (1, 2, 3)])
    with sessions.begin() as session:
        plan = apply_admission_control(session, now=NOW, concurrency=1, run_minutes=45)
        assert plan["counts"] == {"ok": 1, "at_risk": 2, "infeasible": 0}
        assert plan["pending"] == 3
    with sessions.begin() as session:
        runs = session.scalars(select(Run).order_by(Run.next_run_at)).all()
        assert [run.capacity_state for run in runs] == ["ok", "at_risk", "at_risk"]
        assert [_aware(run.next_run_at) for run in runs] == [
            NOW,
            NOW + timedelta(minutes=45),
            NOW + timedelta(minutes=90),
        ]
        assert [_aware(run.start_by_at) for run in runs] == [target - timedelta(minutes=45)] * 3
        # run 1 already defaults to "ok", so only the two late slots produce an audit event.
        events = _capacity_events(session)
        assert sorted(event.event_type for event in events) == ["capacity_at_risk", "capacity_at_risk"]
        assert {event.details["reason"] for event in events} == {"queued_beyond_latest_start"}
    with sessions.begin() as session:
        again = apply_admission_control(session, now=NOW, concurrency=1, run_minutes=45)
        assert again["counts"] == {"ok": 1, "at_risk": 2, "infeasible": 0}
        assert len(_capacity_events(session)) == 2
    engine.dispose()


def test_capacity_report_is_read_only():
    engine, sessions = create_database("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with sessions.begin() as session:
        [blog] = _bootstrap(session)
        session.add_all([_run(blog, NOW + timedelta(hours=1), ordinal=index) for index in (1, 2)])
    with sessions.begin() as session:
        report = capacity_report(session, now=NOW, concurrency=1, run_minutes=45)
        assert report["concurrency"] == 1
        assert report["run_minutes"] == 45
        assert report["pending"] == 2
        assert len(report["at_risk_run_ids"]) == 1
        assert set(report["counts"]) == {"ok", "at_risk", "infeasible"}
    with sessions.begin() as session:
        runs = session.scalars(select(Run)).all()
        assert {run.capacity_state for run in runs} == {"ok"}
        assert {run.start_by_at for run in runs} == {None}
        assert _capacity_events(session) == []
    engine.dispose()


def test_ten_blog_same_target_benchmark_reports_the_lead_time_shortfall():
    """10 profiles at one publish time is a capacity question, not 10 independent runs."""
    engine, sessions = create_database("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    target = NOW + timedelta(minutes=120)
    with sessions.begin() as session:
        blogs = _bootstrap(session, count=10)
        session.add_all([_run(blog, target) for blog in blogs])
    with sessions.begin() as session:
        plan = apply_admission_control(session, now=NOW, concurrency=2, run_minutes=45)
        # 2 lanes x 120 minutes = 240 minutes of capacity for 10 x 45 = 450 minutes of work.
        assert plan["pending"] == 10
        assert plan["counts"] == {"ok": 4, "at_risk": 6, "infeasible": 0}
        assert len(plan["at_risk_run_ids"]) == 6
    engine.dispose()


def test_enqueue_due_slots_freezes_schedule_version_and_plans_the_slot():
    engine, sessions = create_database("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    now = datetime(2026, 11, 6, 2, 30, tzinfo=timezone.utc)  # 09:30 Jakarta
    with sessions.begin() as session:
        [blog] = _bootstrap(
            session,
            active=True,
            validated_revision=1,
            capability_result={"list_posts": True, "history_complete": True},
            schedule_enabled=True,
            schedule_timezone="Asia/Jakarta",
            publish_time="11:00",
            lead_minutes=120,
        )
    with sessions.begin() as session:
        created = enqueue_due_slots(session, now=now, concurrency=1, run_minutes=45)
        assert len(created) == 1
        run = created[0]
        assert run.schedule_version == 1
        assert run.start_by_at is not None
        assert _aware(run.start_by_at) == datetime(2026, 11, 6, 3, 15, tzinfo=timezone.utc)
        assert run.capacity_state == "ok"
        assert _aware(run.target_at) == datetime(2026, 11, 6, 4, 0, tzinfo=timezone.utc)
    with sessions.begin() as session:
        run = session.scalars(select(Run)).one()
        assert (_aware(run.next_run_at)) >= now
        assert _capacity_events(session) == []
    engine.dispose()
