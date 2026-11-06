from __future__ import annotations

from dataclasses import replace

from fastapi.testclient import TestClient

from takumi_research_agent.api.app import create_app
from takumi_research_agent.config import Settings
from takumi_research_agent.persistence.database import create_database
from takumi_research_agent.persistence.models import Base, BlogProfile, GlobalSettings, ProviderProfile


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
