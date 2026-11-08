from __future__ import annotations

import hashlib
from datetime import date, datetime, timedelta, timezone

import pytest

from takumi_research_agent.blogs.adapter import BlogCapabilities, DeliveryOutcomeUnknown
from takumi_research_agent.blogs.contracts import BlogMapping
from takumi_research_agent.delivery.service import DeliveryBlocked, DeliveryService
from takumi_research_agent.persistence.database import create_database
from takumi_research_agent.persistence.models import (
    ArticlePairRecord,
    ArticleVersion,
    Base,
    BlogProfile,
    ContentLedger,
    DeliveryIntent,
    GlobalSettings,
    Run,
)


MAPPING = {
    "list_path": "$.data.items",
    "response_fields": {"id": "$.post.id", "title": "$.post.title", "content": "$.post.content"},
    "create_fields": {"title": "$.article.title", "content": "$.article.body", "locale": "$.article.language", "status": "$.article.state"},
    "publish_status_values": {"draft": "draft", "publish": "published"},
}
LIST_OPERATION = {"operation_id": "listPosts", "method": "get", "path": "/posts"}
CREATE_OPERATION = {"operation_id": "createPost", "method": "post", "path": "/posts"}


class FakeAdapter:
    def __init__(self, mapping: BlogMapping, outcome: str = "ok"):
        self.mapping = mapping
        self.capabilities = BlogCapabilities(list_posts=True, create_draft=True, publish=True, idempotency_key=True)
        self.outcome = outcome
        self.calls: list[dict] = []

    async def send_payload(self, payload, *, intent_key: str, publish: bool):
        self.calls.append({"payload": payload, "intent_key": intent_key, "publish": publish})
        if self.outcome == "unknown":
            raise DeliveryOutcomeUnknown("timeout after POST; remote outcome unknown")
        if self.outcome == "failure":
            raise RuntimeError("sandbox rejected the request")
        return {"http_status": 201, "body": {"post": {"id": f"remote-{len(self.calls)}"}}}


def _environment(*, execution_mode: str = "sandbox", create_draft: bool = True):
    engine, sessions = create_database("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    now = datetime.now(timezone.utc)
    with sessions.begin() as session:
        session.add(GlobalSettings(id=1, recovery_mode=False, content_mutations_paused=False, revision=1))
        session.add(
            BlogProfile(
                id="blog-1",
                name="Sandbox blog",
                base_url="https://blog.example/api",
                adapter_mapping=MAPPING,
                capabilities={"list_operation": LIST_OPERATION, "create_operation": CREATE_OPERATION},
                capability_result={"list_posts": True, "history_complete": True, "create_draft": create_draft, "idempotency_key": True},
                revision=3,
                destination_epoch=1,
                active=True,
            )
        )
        session.add(
            Run(
                id="run-1",
                blog_id="blog-1",
                editorial_date=date(2026, 11, 7),
                quota_ordinal=1,
                quota_slot_key="slot-1",
                target_at=now + timedelta(hours=1),
                execution_mode=execution_mode,
                config_snapshot={"blog_revision": 3, "destination_epoch": 1},
                status="completed",
                stage="completed",
            )
        )
        session.add(
            ArticlePairRecord(
                id="pair-1",
                run_id="run-1",
                topic="A long enough science topic",
                field="physics",
                research_question="What does the result actually show?",
                decision="draft",
                review_result={"approved": True, "reader_score": 4},
            )
        )
        for locale, title in (("id", "Judul artikel sains"), ("en", "A science article title")):
            body = f"Final body for {locale} with enough characters to be realistic." * 10
            session.add(
                ArticleVersion(
                    pair_id="pair-1",
                    locale=locale,
                    version=1,
                    title=title,
                    body_markdown=body,
                    final_body=body,
                    body_hash=hashlib.sha256(body.encode()).hexdigest(),
                    metadata_json={"summary": f"Summary {locale}"},
                )
            )
            session.add(
                ContentLedger(
                    blog_id="blog-1",
                    pair_id="pair-1",
                    topic_fingerprint="a" * 64,
                    locale=locale,
                    unit_key=f"article:{locale}",
                    local_state="draft_saved",
                    remote_status="not_sent",
                )
            )
    return sessions


def _intents(sessions):
    with sessions() as session:
        return session.query(DeliveryIntent).order_by(DeliveryIntent.unit_key).all()


def test_prepare_creates_one_intent_per_locale_and_is_idempotent():
    sessions = _environment()
    service = DeliveryService()
    with sessions.begin() as session:
        created = service.prepare(session, run_id="run-1")
    assert len(created) == 2
    first = {intent.unit_key: (intent.delivery_key, intent.approval_fingerprint) for intent in _intents(sessions)}
    assert set(first) == {"article:id", "article:en"}
    assert len({key for key, _ in first.values()}) == 2

    with sessions.begin() as session:
        assert service.prepare(session, run_id="run-1") == []
    second = {intent.unit_key: (intent.delivery_key, intent.approval_fingerprint) for intent in _intents(sessions)}
    assert first == second


@pytest.mark.asyncio
async def test_dispatch_success_records_attempt_remote_id_and_is_not_resent():
    sessions = _environment()
    service = DeliveryService()
    with sessions.begin() as session:
        service.prepare(session, run_id="run-1")
    adapter = FakeAdapter(BlogMapping.model_validate(MAPPING))

    with sessions() as session:
        summary = await service.dispatch(session, run_id="run-1", adapter=adapter)
    assert summary["succeeded"] == 2
    assert len(adapter.calls) == 2
    assert all(call["intent_key"].startswith("takumi:") for call in adapter.calls)
    assert all(call["publish"] is False for call in adapter.calls)

    with sessions() as session:
        intent = session.query(DeliveryIntent).filter_by(unit_key="article:id").one()
        ledger = session.query(ContentLedger).filter_by(unit_key="article:id").one()
        assert intent.state == "succeeded"
        assert intent.remote_id
        assert ledger.local_state == "delivered"
        assert ledger.remote_status == "accepted_unverified"
        assert ledger.remote_id == intent.remote_id

    with sessions() as session:
        again = await service.dispatch(session, run_id="run-1", adapter=adapter)
    assert again["succeeded"] == 0
    assert len(adapter.calls) == 2


@pytest.mark.asyncio
async def test_pause_blocks_dispatch_before_any_network_call():
    sessions = _environment()
    service = DeliveryService()
    with sessions.begin() as session:
        service.prepare(session, run_id="run-1")
        session.get(GlobalSettings, 1).content_mutations_paused = True
    adapter = FakeAdapter(BlogMapping.model_validate(MAPPING))

    with sessions() as session:
        with pytest.raises(DeliveryBlocked, match="content_mutations_paused"):
            await service.dispatch(session, run_id="run-1", adapter=adapter)
    assert adapter.calls == []
    assert {intent.state for intent in _intents(sessions)} == {"prepared"}


@pytest.mark.asyncio
async def test_dryrun_never_delivers():
    sessions = _environment(execution_mode="dryrun")
    service = DeliveryService()
    with sessions.begin() as session:
        service.prepare(session, run_id="run-1")
    adapter = FakeAdapter(BlogMapping.model_validate(MAPPING))

    with sessions() as session:
        with pytest.raises(DeliveryBlocked, match="dryrun_never_delivers"):
            await service.dispatch(session, run_id="run-1", adapter=adapter)
    assert adapter.calls == []


@pytest.mark.asyncio
async def test_changed_article_body_invalidates_the_approval():
    sessions = _environment()
    service = DeliveryService()
    with sessions.begin() as session:
        service.prepare(session, run_id="run-1")
    adapter = FakeAdapter(BlogMapping.model_validate(MAPPING))
    with sessions.begin() as session:
        version = session.query(ArticleVersion).filter_by(locale="en").one()
        version.final_body = "Silently edited body that was never reviewed."
        version.body_hash = hashlib.sha256(version.final_body.encode()).hexdigest()

    with sessions() as session:
        summary = await service.dispatch(session, run_id="run-1", adapter=adapter)
    assert adapter.calls == []
    assert {item["code"] for item in summary["blocked"]} == {"approval_invalidated"}
    assert {intent.state for intent in _intents(sessions)} == {"invalidated"}


@pytest.mark.asyncio
async def test_changed_evidence_invalidates_the_approval():
    sessions = _environment()
    service = DeliveryService()
    with sessions.begin() as session:
        service.prepare(session, run_id="run-1")
    adapter = FakeAdapter(BlogMapping.model_validate(MAPPING))
    with sessions.begin() as session:
        pair = session.get(ArticlePairRecord, "pair-1")
        pair.review_result = {**pair.review_result, "evidence": [{"evidence_id": "e-1", "source_id": "doi:1", "content_hash": "b" * 64}]}

    with sessions() as session:
        summary = await service.dispatch(session, run_id="run-1", adapter=adapter)
    assert adapter.calls == []
    assert {item["code"] for item in summary["blocked"]} == {"approval_invalidated"}


@pytest.mark.asyncio
async def test_unknown_outcome_is_not_retried_automatically():
    sessions = _environment()
    service = DeliveryService()
    with sessions.begin() as session:
        service.prepare(session, run_id="run-1")
    adapter = FakeAdapter(BlogMapping.model_validate(MAPPING), outcome="unknown")

    with sessions() as session:
        first = await service.dispatch(session, run_id="run-1", adapter=adapter)
    assert first["unknown"] == 2
    assert len(adapter.calls) == 2
    assert {intent.state for intent in _intents(sessions)} == {"unknown"}
    with sessions() as session:
        ledger = session.query(ContentLedger).filter_by(unit_key="article:id").one()
        assert ledger.local_state == "delivery_unknown"
        assert ledger.remote_status == "unknown"

    with sessions() as session:
        second = await service.dispatch(session, run_id="run-1", adapter=adapter)
    assert second["unknown"] == 0
    assert len(adapter.calls) == 2


@pytest.mark.asyncio
async def test_unproven_draft_capability_keeps_the_intent_local():
    sessions = _environment(create_draft=False)
    service = DeliveryService()
    with sessions.begin() as session:
        service.prepare(session, run_id="run-1")
    adapter = FakeAdapter(BlogMapping.model_validate(MAPPING))

    with sessions() as session:
        summary = await service.dispatch(session, run_id="run-1", adapter=adapter)
    assert adapter.calls == []
    assert {item["code"] for item in summary["blocked"]} == {"create_draft_capability_unproven"}
    assert {intent.state for intent in _intents(sessions)} == {"prepared"}
