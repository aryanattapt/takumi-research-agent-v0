from __future__ import annotations

import json
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone

import httpx
from fastapi.testclient import TestClient

import takumi_research_agent.api.app as api_module
from takumi_research_agent.api.app import create_app
from takumi_research_agent.config import Settings
from takumi_research_agent.delivery.service import STALE_DISPATCH_AFTER
from takumi_research_agent.persistence.database import create_database
from takumi_research_agent.persistence.models import ArticlePairRecord, ArticleVersion, Base, BlogProfile, DeliveryAttempt, DeliveryIntent, EvidenceSpanRecord, GlobalSettings, ProviderProfile, Run, RunEvent, SourceRecord as SourceRecordRow
from takumi_research_agent.security import SecretCipher


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


def test_run_detail_exposes_agent_trace_claim_links_and_verifiable_quotes():
    engine, sessions = create_database("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    settings = replace(Settings.from_env(env_file=None), master_key="test-master-key-" * 4, session_cookie_secure=False)
    app = create_app(settings, engine=engine, sessions=sessions)
    content_hash = "a" * 64
    with sessions.begin() as db:
        blog = BlogProfile(id="blog-detail", name="Trace blog", base_url="https://blog.example/api")
        db.add(blog)
        db.flush()
        db.add(Run(
            id="run-detail",
            blog_id=blog.id,
            editorial_date=date(2026, 11, 10),
            quota_slot_key="slot-detail",
            target_at=datetime.now(timezone.utc) + timedelta(hours=1),
            execution_mode="dryrun",
            config_snapshot={},
            status="completed",
            stage="completed",
        ))
        db.add(RunEvent(
            run_id="run-detail",
            event_type="discovery_coverage",
            details={"coverage": {
                "window_start": "2026-11-09T10:00:00+00:00",
                "baseline_start": "2026-11-07T10:00:00+00:00",
                "observed_at": "2026-11-10T10:00:00+00:00",
                "candidate_count": 3,
                "sources": {"rss": {"configured": 1, "successful": 1, "failed": 0, "items": 3, "feeds": []}},
            }},
        ))
        db.add(SourceRecordRow(
            id="doi:10.5555/detail",
            url="https://journal.example/detail",
            title="Controlled study of measured cells",
            source_type="journal",
            metadata_json={"peer_reviewed": True, "peer_review_basis": "journal_policy", "read_status": "full_text"},
        ))
        db.add(ArticlePairRecord(
            id="pair-detail",
            run_id="run-detail",
            topic="A measured change in cell survival",
            field="biology",
            research_question="What did the controlled study measure?",
            decision="draft",
            review_result={
                "approved": True,
                "reader_score": 4,
                "material_claims_supported": True,
                "bilingual_parity": True,
                "findings": [],
                "agent": {
                    "physical_requests": 3,
                    "research_physical_requests": 2,
                    "tool_actions": 1,
                    "trace": [{"type": "tool", "name": "sources.read", "status": "ok"}],
                },
                "sources": [{"source_id": "doi:10.5555/detail", "content_hash": content_hash, "read_status": "full_text", "retracted": False}],
                "evidence": [{"evidence_id": "evidence-detail-1", "source_id": "doi:10.5555/detail", "content_hash": content_hash}],
                "claims": [{"claim": "Measured cell survival increased in the study.", "evidence_ids": ["evidence-detail-1"], "importance": "critical", "support": "supported"}],
            },
        ))
        db.add(EvidenceSpanRecord(
            id="evidence-detail-1",
            source_id="doi:10.5555/detail",
            content_hash=content_hash,
            quote="Measured cell survival increased under controlled conditions.",
            locator="Results, paragraph 2",
            artifact_path="/tmp/not-read-by-detail-endpoint",
        ))
        db.add(ArticleVersion(
            pair_id="pair-detail",
            locale="en",
            version=1,
            title="What the cell study measured",
            body_markdown="Article body",
            final_body="Article body",
            body_hash="b" * 64,
            metadata_json={"summary": "An explanation for readers.", "cited_evidence_ids": ["evidence-detail-1"]},
        ))

    with TestClient(app) as client:
        bootstrap = client.post("/api/v1/setup/admin", json={"username": "admin", "password": "correct-horse-battery-staple"})
        assert bootstrap.status_code == 201
        login = client.post("/api/v1/auth/login", json={"username": "admin", "password": "correct-horse-battery-staple"})
        detail = client.get("/api/v1/runs/run-detail")
        assert detail.status_code == 200
        pair = detail.json()["pair"]
        assert detail.json()["discovery_coverage"]["candidate_count"] == 3
        assert detail.json()["discovery_coverage"]["sources"]["rss"]["successful"] == 1
        assert "agent" not in pair["review"]
        assert pair["agent"]["trace"] == [{"type": "tool", "name": "sources.read", "status": "ok"}]
        assert pair["claims"][0]["evidence_ids"] == ["evidence-detail-1"]
        assert pair["sources"][0]["title"] == "Controlled study of measured cells"
        assert pair["sources"][0]["available"] is True
        assert pair["evidence"][0]["quote"] == "Measured cell survival increased under controlled conditions."
        assert pair["articles"][0]["cited_evidence_ids"] == ["evidence-detail-1"]
    engine.dispose()


def test_sandbox_create_capability_requires_ack_pause_clear_and_posts_draft(monkeypatch):
    engine, sessions = create_database("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    master_key = "test-master-key-" * 4
    settings = replace(Settings.from_env(env_file=None), master_key=master_key, session_cookie_secure=False)
    app = create_app(settings, engine=engine, sessions=sessions)
    mapping = {
        "list_path": "$.data.items",
        "response_fields": {"id": "$.post.id", "title": "$.post.title", "content": "$.post.content"},
        "create_fields": {"title": "$.article.title", "content": "$.article.body", "locale": "$.article.language", "status": "$.article.state"},
        "publish_status_values": {"draft": "drafting", "publish": "live"},
    }
    with sessions.begin() as db:
        db.add(GlobalSettings(id=1, recovery_mode=False, content_mutations_paused=True, revision=1))
        db.add(BlogProfile(
            id="blog-sandbox",
            name="Sandbox blog",
            base_url="https://sandbox.example/api",
            encrypted_api_token=SecretCipher(master_key).encrypt("sandbox-token"),
            adapter_mapping=mapping,
            capabilities={
                "list_operation": {"operation_id": "listPosts", "method": "get", "path": "/posts"},
                "create_operation": {"operation_id": "createPost", "method": "post", "path": "/posts"},
            },
            capability_result={"list_posts": True, "history_complete": True},
        ))

    seen = []

    def fake_blog(request: httpx.Request):
        seen.append({
            "url": str(request.url),
            "authorization": request.headers.get("Authorization"),
            "payload": json.loads(request.content),
            "idempotency_key": request.headers.get("Idempotency-Key"),
        })
        return httpx.Response(201, json={"post": {"id": "sandbox-draft-42"}})

    adapter_type = api_module.BlogApiAdapter
    monkeypatch.setattr(api_module, "BlogApiAdapter", lambda **kwargs: adapter_type(**kwargs, transport=httpx.MockTransport(fake_blog)))
    with TestClient(app) as client:
        bootstrap = client.post("/api/v1/setup/admin", json={"username": "admin", "password": "correct-horse-battery-staple"})
        assert bootstrap.status_code == 201
        login = client.post("/api/v1/auth/login", json={"username": "admin", "password": "correct-horse-battery-staple"})
        csrf = login.json()["csrf_token"]
        path = "/api/v1/blogs/blog-sandbox/adapter/test-create"

        missing_ack = client.post(path, headers={"X-CSRF-Token": csrf}, json={"acknowledge_remote_write": False})
        assert missing_ack.status_code == 409
        while_paused = client.post(path, headers={"X-CSRF-Token": csrf}, json={"acknowledge_remote_write": True})
        assert while_paused.status_code == 409
        assert seen == []

        with sessions.begin() as db:
            control = db.get(GlobalSettings, 1)
            control.recovery_mode = True
            control.content_mutations_paused = False
        while_recovering = client.post(path, headers={"X-CSRF-Token": csrf}, json={"acknowledge_remote_write": True})
        assert while_recovering.status_code == 409
        assert seen == []

        with sessions.begin() as db:
            control = db.get(GlobalSettings, 1)
            control.recovery_mode = False
            control.content_mutations_paused = False
        passed = client.post(path, headers={"X-CSRF-Token": csrf}, json={"acknowledge_remote_write": True})
        assert passed.status_code == 200, passed.text
        assert passed.json()["create_draft"] is True
        assert passed.json()["remote_id"] == "sandbox-draft-42"
        assert len(seen) == 1
        assert seen[0]["url"] == "https://sandbox.example/api/posts"
        assert seen[0]["authorization"] == "Bearer sandbox-token"
        assert seen[0]["payload"]["article"]["state"] == "drafting"
        assert "Takumi capability check" in seen[0]["payload"]["article"]["title"]
        assert passed.json()["request_payload"] == seen[0]["payload"]
        with sessions() as db:
            capability = db.get(BlogProfile, "blog-sandbox").capability_result
            assert capability["sandbox_create"] is True
            assert capability["create_remote_id"] == "sandbox-draft-42"
    engine.dispose()
