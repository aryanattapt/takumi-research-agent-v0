from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone

from fastapi.testclient import TestClient

from takumi_research_agent.api.app import create_app
from takumi_research_agent.config import Settings
from takumi_research_agent.delivery.service import STALE_DISPATCH_AFTER
from takumi_research_agent.persistence.database import create_database
from takumi_research_agent.persistence.models import Base, BlogProfile, DeliveryAttempt, DeliveryIntent, GlobalSettings, ProviderProfile, Run


def test_setup_login_csrf_and_ten_blog_limit():
    engine, sessions = create_database("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    settings = replace(Settings.from_env(env_file=None), master_key="test-master-key-" * 4, session_cookie_secure=False)
    app = create_app(settings, engine=engine, sessions=sessions)
    with TestClient(app) as client:
        bootstrap = client.post("/api/v1/setup/admin", json={"username": "admin", "password": "correct-horse-battery-staple"})
        assert bootstrap.status_code == 201
        login = client.post("/api/v1/auth/login", json={"username": "admin", "password": "correct-horse-battery-staple"})
        assert login.status_code == 200
        csrf = login.json()["csrf_token"]
        assert client.get("/api/v1/blogs").status_code == 200
        for index in range(10):
            response = client.post(
                "/api/v1/blogs",
                headers={"X-CSRF-Token": csrf},
                json={
                    "name": f"Blog {index}",
                    "base_url": f"https://blog-{index}.example/api",
                    "api_spec_source": "",
                    "schedule_enabled": False,
                    "schedule_timezone": "Asia/Jakarta",
                    "publish_time": "06:00",
                    "lead_minutes": 120,
                },
            )
            assert response.status_code == 201, response.text
        eleventh = client.post(
            "/api/v1/blogs",
            headers={"X-CSRF-Token": csrf},
            json={"name": "Blog 11", "base_url": "https://another.example/api"},
        )
        assert eleventh.status_code == 409
        profiles = client.get("/api/v1/blogs").json()
        assert len(profiles) == 10
        assert all(not item["active"] for item in profiles)
        assert all(not item["secret_configured"] for item in profiles)
    engine.dispose()


def test_mutation_routes_require_csrf():
    engine, sessions = create_database("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    settings = replace(Settings.from_env(env_file=None), master_key="test-master-key-" * 4, session_cookie_secure=False)
    app = create_app(settings, engine=engine, sessions=sessions)
    with TestClient(app) as client:
        client.post("/api/v1/setup/admin", json={"username": "admin", "password": "correct-horse-battery-staple"})
        client.post("/api/v1/auth/login", json={"username": "admin", "password": "correct-horse-battery-staple"})
        response = client.put("/api/v1/control", json={"content_mutations_paused": False, "expected_revision": 1})
        assert response.status_code == 403
    engine.dispose()


def test_capacity_endpoint_reports_queue_and_run_fields(monkeypatch):
    monkeypatch.setenv("WORKER_CONCURRENCY", "2")
    monkeypatch.setenv("CAPACITY_RUN_MINUTES", "45")
    engine, sessions = create_database("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    settings = replace(Settings.from_env(env_file=None), master_key="test-master-key-" * 4, session_cookie_secure=False)
    app = create_app(settings, engine=engine, sessions=sessions)
    with sessions.begin() as db:
        db.add(GlobalSettings(id=1, recovery_mode=False, content_mutations_paused=True))
        provider = ProviderProfile(
            name="Test model",
            provider_type="openai_compatible",
            base_url="https://model.example/v1",
            model="test-model",
            encrypted_api_key="",
            capability_result={"status": "reachable"},
        )
        db.add(provider)
        db.flush()
        db.add(BlogProfile(
            name="Capacity blog",
            base_url="https://blog.example/api",
            provider_id=provider.id,
            capability_result={"list_posts": True, "history_complete": True},
            validated_revision=1,
            active=True,
        ))
    with TestClient(app) as client:
        client.post("/api/v1/setup/admin", json={"username": "admin", "password": "correct-horse-battery-staple"})
        csrf = client.post("/api/v1/auth/login", json={"username": "admin", "password": "correct-horse-battery-staple"}).json()["csrf_token"]
        blog = client.get("/api/v1/blogs").json()[0]
        queued = client.post(f"/api/v1/blogs/{blog['id']}/runs", headers={"X-CSRF-Token": csrf})
        assert queued.status_code == 202, queued.text
        assert queued.json()["capacity_state"] == "ok"
        capacity = client.get("/api/v1/capacity")
        assert capacity.status_code == 200
        plan = capacity.json()
        assert plan["concurrency"] == 2
        assert plan["run_minutes"] == 45
        assert plan["pending"] == 1
        assert plan["counts"] == {"ok": 1, "at_risk": 0, "infeasible": 0}
        assert plan["slots"][0]["state"] == "ok"
        runs = client.get("/api/v1/runs").json()
        assert runs[0]["capacity_state"] == "ok"
        assert runs[0]["start_by_at"] is not None
        assert runs[0]["schedule_version"] == 1
        detail = client.get(f"/api/v1/runs/{queued.json()['run_id']}").json()
        assert detail["capacity_state"] == "ok"
        assert detail["start_by_at"] == runs[0]["start_by_at"]
    engine.dispose()


def test_manual_run_is_queued_as_dryrun_and_details_are_readable():
    engine, sessions = create_database("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    master_key = "test-master-key-" * 4
    settings = replace(Settings.from_env(env_file=None), master_key=master_key, session_cookie_secure=False)
    app = create_app(settings, engine=engine, sessions=sessions)
    with sessions.begin() as db:
        control = db.get(GlobalSettings, 1)
        if control is None:
            db.add(GlobalSettings(id=1, recovery_mode=False, content_mutations_paused=True))
        provider = ProviderProfile(
            name="Test model",
            provider_type="openai_compatible",
            base_url="https://model.example/v1",
            model="test-model",
            encrypted_api_key="",
            capability_result={"status": "reachable"},
        )
        db.add(provider)
        db.flush()
        db.add(BlogProfile(
            name="Test blog",
            base_url="https://blog.example/api",
            provider_id=provider.id,
            adapter_mapping={
                "list_path": "$.data.items",
                "response_fields": {"id": "$.id", "title": "$.title", "content": "$.content"},
                "create_fields": {"title": "$.title", "content": "$.content"},
            },
            capabilities={"list_operation": {"operation_id": "listPosts", "method": "get", "path": "/posts"}},
            capability_result={"list_posts": True, "history_complete": True},
            validated_revision=1,
            active=True,
        ))
    with TestClient(app) as client:
        client.post("/api/v1/setup/admin", json={"username": "admin", "password": "correct-horse-battery-staple"})
        csrf = client.post("/api/v1/auth/login", json={"username": "admin", "password": "correct-horse-battery-staple"}).json()["csrf_token"]
        blog = client.get("/api/v1/blogs").json()[0]
        queued = client.post(f"/api/v1/blogs/{blog['id']}/runs", headers={"X-CSRF-Token": csrf})
        assert queued.status_code == 202, queued.text
        assert queued.json()["execution_mode"] == "dryrun"
        detail = client.get(f"/api/v1/runs/{queued.json()['run_id']}")
        assert detail.status_code == 200
        assert detail.json()["status"] == "queued"
        assert detail.json()["pair"] is None
    engine.dispose()


def test_recovery_endpoint_reports_artifact_integrity_and_last_restore(tmp_path):
    engine, sessions = create_database("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    settings = replace(
        Settings.from_env(env_file=None),
        master_key="test-master-key-" * 4,
        session_cookie_secure=False,
        data_dir=tmp_path / "data",
    )
    app = create_app(settings, engine=engine, sessions=sessions)
    with TestClient(app) as client:
        client.post("/api/v1/setup/admin", json={"username": "admin", "password": "correct-horse-battery-staple"})
        client.post("/api/v1/auth/login", json={"username": "admin", "password": "correct-horse-battery-staple"})
        response = client.get("/api/v1/recovery")
        assert response.status_code == 200
        body = response.json()
        assert body["recovery_mode"] is False
        assert body["artifacts"] == {"checked": False, "total": 0, "verified": None, "missing": [], "corrupt": [], "truncated": False}
        checked = client.get("/api/v1/recovery?verify_artifacts=true")
        assert checked.status_code == 200
        assert checked.json()["artifacts"]["checked"] is True
        assert checked.json()["artifacts"]["verified"] == 0
        assert body["last_restore"] is None
    engine.dispose()


def test_reconciliation_waits_for_stale_dispatch_and_finalizes_attempt():
    engine, sessions = create_database("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    settings = replace(Settings.from_env(env_file=None), master_key="test-master-key-" * 4, session_cookie_secure=False)
    app = create_app(settings, engine=engine, sessions=sessions)
    started_at = datetime.now(timezone.utc)
    with sessions.begin() as db:
        blog = BlogProfile(id="blog-1", name="Test blog", base_url="https://blog.example/api")
        db.add(blog)
        db.flush()
        run = Run(
            id="run-1",
            blog_id=blog.id,
            editorial_date=date(2026, 11, 10),
            quota_slot_key="slot-1",
            target_at=started_at + timedelta(hours=1),
            execution_mode="sandbox",
            config_snapshot={},
            status="completed",
            stage="completed",
        )
        db.add(run)
        db.flush()
        intent = DeliveryIntent(
            id="intent-1",
            run_id=run.id,
            blog_id=blog.id,
            pair_id="pair-1",
            unit_key="article:id",
            action="draft",
            operation_id="createPost",
            destination_epoch=1,
            delivery_key="d" * 64,
            approval_fingerprint="a" * 64,
            payload={"title": "A reviewed title"},
            state="dispatching",
        )
        db.add(intent)
        db.flush()
        db.add(DeliveryAttempt(
            id="attempt-1",
            intent_id=intent.id,
            attempt_number=1,
            started_at=started_at,
            outcome="in_flight",
        ))

    with TestClient(app) as client:
        client.post("/api/v1/setup/admin", json={"username": "admin", "password": "correct-horse-battery-staple"})
        csrf = client.post("/api/v1/auth/login", json={"username": "admin", "password": "correct-horse-battery-staple"}).json()["csrf_token"]
        detail = client.get("/api/v1/runs/run-1").json()
        assert detail["deliveries"][0]["reconciliation_ready"] is False

        early = client.post(
            "/api/v1/runs/run-1/delivery/intent-1/reconcile",
            headers={"X-CSRF-Token": csrf},
            json={"outcome": "succeeded", "remote_id": "remote-1"},
        )
        assert early.status_code == 409
        with sessions() as db:
            assert db.get(DeliveryIntent, "intent-1").state == "dispatching"
            assert db.get(DeliveryAttempt, "attempt-1").outcome == "in_flight"

        with sessions.begin() as db:
            db.get(DeliveryAttempt, "attempt-1").started_at = datetime.now(timezone.utc) - STALE_DISPATCH_AFTER - timedelta(seconds=1)
        detail = client.get("/api/v1/runs/run-1").json()
        assert detail["deliveries"][0]["reconciliation_ready"] is True
        resolved = client.post(
            "/api/v1/runs/run-1/delivery/intent-1/reconcile",
            headers={"X-CSRF-Token": csrf},
            json={"outcome": "succeeded", "remote_id": "remote-1", "note": "Confirmed in blog history"},
        )
        assert resolved.status_code == 200, resolved.text
        duplicate = client.post(
            "/api/v1/runs/run-1/delivery/intent-1/reconcile",
            headers={"X-CSRF-Token": csrf},
            json={"outcome": "failed", "note": "Conflicting second reconciliation"},
        )
        assert duplicate.status_code == 409

    with sessions() as db:
        intent = db.get(DeliveryIntent, "intent-1")
        attempt = db.get(DeliveryAttempt, "attempt-1")
        assert intent.state == "succeeded" and intent.remote_id == "remote-1"
        assert attempt.outcome == "succeeded" and attempt.finished_at is not None
        assert attempt.response_summary["reconciled"] is True
        assert attempt.response_summary["note"] == "Confirmed in blog history"
    engine.dispose()
