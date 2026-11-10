from __future__ import annotations

import hashlib
import json
from collections import deque
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

from takumi_research_agent.config import Settings
from takumi_research_agent.blogs.adapter import HistoryBatch
from takumi_research_agent.delivery.service import DeliveryService
from takumi_research_agent.discovery.trends import TrendCandidate
from takumi_research_agent.orchestration import pipeline as pipeline_module
from takumi_research_agent.orchestration.scheduler import RunWorker
from takumi_research_agent.persistence.database import create_database
from takumi_research_agent.persistence.models import (
    ArticlePairRecord,
    ArticleVersion,
    Base,
    BlogProfile,
    ContentLedger,
    DeliveryAttempt,
    DeliveryIntent,
    EvidenceSpanRecord,
    GlobalSettings,
    ProviderProfile,
    Run,
    RunEvent,
    SourceRecord,
    TrendObservation,
)
from takumi_research_agent.providers.base import ModelReply, ToolCall


TOPIC = "Cell survival changes after a measured treatment"
SOURCE_ID = "doi:10.5555/cell-survival-2026"
SOURCE_TEXT = (
    "Under controlled conditions, the treatment increased cell survival by twelve percent. "
    "The effect appeared in three independent laboratory samples. "
    "Results were measured after forty-eight hours and were not tested in humans."
)
QUOTES = (
    "the treatment increased cell survival by twelve percent",
    "appeared in three independent laboratory samples",
    "measured after forty-eight hours and were not tested in humans",
)


class SimulatedWorkerCrash(BaseException):
    """A process-level crash must bypass RunWorker's normal exception finalizer."""


def _evidence_ids() -> list[str]:
    content_hash = hashlib.sha256(SOURCE_TEXT.encode()).hexdigest()
    result = []
    for quote in QUOTES:
        normalized = " ".join(quote.split()).casefold()
        quote_hash = hashlib.sha256(normalized.encode()).hexdigest()
        result.append(hashlib.sha256(f"{SOURCE_ID}\0{content_hash}\0{quote_hash}".encode()).hexdigest())
    return result


def _provider_replies() -> list[ModelReply]:
    evidence_ids = _evidence_ids()
    calls = [
        ToolCall("search", "literature.search", {"query": TOPIC, "source": "both", "max_results": 5}),
        ToolCall("read", "sources.read", {"source_id": SOURCE_ID, "offset": 0, "max_chars": 8_000}),
        ToolCall("status", "sources.check_status", {"source_id": SOURCE_ID}),
        ToolCall("history", "history.find_related", {"topic": TOPIC}),
        *[
            ToolCall(
                f"evidence-{index}",
                "evidence.record",
                {"source_id": SOURCE_ID, "claim": f"Supported finding number {index}.", "quote": quote},
            )
            for index, quote in enumerate(QUOTES, start=1)
        ],
        ToolCall(
            "finish",
            "research.finish",
            {
                "field": "biology",
                "topic": TOPIC,
                "research_question": "How did the treatment affect cell survival?",
                "findings": ["Survival increased in the measured samples.", "The study did not test human outcomes."],
                "source_ids": [SOURCE_ID],
                "relevant_source_ids": [SOURCE_ID],
                "evidence_ids": evidence_ids,
                "claims": [
                    {
                        "claim": f"Supported finding number {index}.",
                        "evidence_ids": [evidence_id],
                        "importance": "material",
                        "support": "supported",
                    }
                    for index, evidence_id in enumerate(evidence_ids, start=1)
                ],
                "novelty_clear": True,
                "novelty_reason": "No related item appears in the complete fixture history.",
                "credible_sources": True,
            },
        ),
    ]
    replies = [ModelReply("", (call,), "tool_call") for call in calls]
    replies.append(ModelReply("Research bundle is complete."))
    body_id = "Penelitian ini mengukur perubahan pada sampel sel dalam kondisi terkontrol. " * 9
    body_en = "This study measured changes in cell samples under controlled conditions. " * 9
    replies.append(
        ModelReply(
            json.dumps(
                {
                    "articles": {
                        "id": {
                            "title": "Temuan tentang kelangsungan hidup sel",
                            "summary": "Ringkasan temuan sel yang menjelaskan batas pengukuran dan konteks penelitian.",
                            "body_markdown": body_id,
                            "cited_evidence_ids": evidence_ids,
                        },
                        "en": {
                            "title": "What the cell survival study found",
                            "summary": "A summary of the cell study that explains the measurement and its limits.",
                            "body_markdown": body_en,
                            "cited_evidence_ids": evidence_ids,
                        },
                    }
                }
            )
        )
    )
    replies.append(
        ModelReply(
            json.dumps(
                {
                    "approved": True,
                    "reader_score": 5,
                    "material_claims_supported": True,
                    "bilingual_parity": True,
                    "findings": [],
                }
            )
        )
    )
    return replies


class FixtureState:
    def __init__(self):
        self.replies = deque(_provider_replies())
        self.crash_first_model_request = True
        self.crash_before_outbox_once = True
        self.provider_requests = 0
        self.trend_collections = 0
        self.blog_history_reads = 0
        self.literature_searches = 0
        self.source_reads = 0
        self.editorial_checks = 0
        self.trend = TrendCandidate(
            title=TOPIC,
            url="https://news.example/item/cell-survival",
            field="biology",
            source="fixture",
            source_item_id="trend-cell-survival-1",
            observed_at=datetime(2026, 11, 10, 10, 0, tzinfo=timezone.utc),
            published_at=datetime.now(timezone.utc) - timedelta(minutes=15),
            raw_metrics={"mentions": 4},
            score=0.82,
            score_details={"label": "fixture"},
        )


class FakeProvider:
    def __init__(self, _config, state: FixtureState):
        self.state = state

    async def complete(self, *_args, **_kwargs):
        self.state.provider_requests += 1
        if self.state.crash_first_model_request:
            self.state.crash_first_model_request = False
            raise SimulatedWorkerCrash("worker stopped after trend persistence")
        return self.state.replies.popleft()

    async def close(self):
        return None


class FakeBlogAdapter:
    def __init__(self, *, state: FixtureState, **_kwargs):
        self.state = state

    async def list_posts(self):
        self.state.blog_history_reads += 1
        return []

    @property
    def history_mapping_hash(self):
        return "f" * 64

    async def read_history_batch(self, *, checkpoint=None, max_pages=1):
        self.state.blog_history_reads += 1
        return HistoryBatch(records=(), checkpoint=None, complete=True, pages_read=1)

    async def close(self):
        return None


class FakeTrendCollector:
    def __init__(self, state: FixtureState):
        self.state = state
        self.last_coverage = {
            "window_start": "2026-11-09T00:00:00+00:00",
            "baseline_start": "2026-11-07T00:00:00+00:00",
            "observed_at": "2026-11-10T00:00:00+00:00",
            "candidate_count": 1,
            "sources": {"fixture": {"configured": 1, "successful": 1, "failed": 0, "items": 1}},
        }

    async def collect(self):
        self.state.trend_collections += 1
        return [self.state.trend]

    async def close(self):
        return None


class FakeLiteratureSources:
    def __init__(self, *, state: FixtureState, **_kwargs):
        self.state = state
        self.paper = {
            "doi": SOURCE_ID.removeprefix("doi:"),
            "title": "A controlled cell survival study",
            "authors": ["Fixture Author"],
            "venue": "Fixture Journal",
            "publication_date": "2026-11-01",
            "type": "journal-article",
            "url": "https://journal.example/article",
            "abstract": SOURCE_TEXT,
            "retracted": False,
        }

    async def search(self, *_args, **_kwargs):
        self.state.literature_searches += 1
        return [self.paper]

    async def read(self, _doi):
        self.state.source_reads += 1
        return {
            "title": self.paper["title"],
            "url": self.paper["url"],
            "venue": self.paper["venue"],
            "publication_date": self.paper["publication_date"],
            "content": SOURCE_TEXT,
            "content_source": "Europe PMC full text",
        }

    def _get_paper(self, _doi):
        return self.paper

    async def verify(self, _doi):
        self.state.editorial_checks += 1
        return {"status": "journal_policy_not_confirmed", "evidence": []}

    async def close(self):
        return None


class FakeRetriever:
    def __init__(self, *_args, **_kwargs):
        pass

    async def close(self):
        return None


@pytest.mark.asyncio
async def test_trend_to_article_worker_replay_recovers_without_duplicate_records(tmp_path, monkeypatch):
    engine, sessions = create_database("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    state = FixtureState()
    settings = replace(
        Settings.from_env(env_file=None),
        master_key="fixture-master-key-that-is-long-enough",
        data_dir=tmp_path,
    )
    with sessions.begin() as session:
        session.add(GlobalSettings(id=1, recovery_mode=False, content_mutations_paused=False))
        provider = ProviderProfile(
            id="provider-1",
            name="Fixture provider",
            provider_type="openai_compatible",
            base_url="https://model.example/v1",
            model="fixture-model",
            encrypted_api_key="",
            capability_result={"status": "reachable"},
            revision=2,
            active=True,
        )
        session.add(provider)
        session.add(
            BlogProfile(
                id="blog-1",
                name="Fixture blog",
                base_url="https://blog.example/api",
                provider_id=provider.id,
                adapter_mapping={
                    "list_path": "$.data.items",
                    "response_fields": {"id": "$.post.id", "title": "$.post.title", "content": "$.post.content"},
                    "create_fields": {"title": "$.article.title", "content": "$.article.body", "locale": "$.article.language", "status": "$.article.state"},
                    "publish_status_values": {"draft": "draft", "publish": "published"},
                },
                capabilities={
                    "list_operation": {"operation_id": "listPosts", "method": "get", "path": "/posts"},
                    "create_operation": {"operation_id": "createPost", "method": "post", "path": "/posts"},
                },
                capability_result={"list_posts": True, "history_complete": True, "create_draft": True, "idempotency_key": True},
                topic_settings={"biology": True},
                editorial_settings={},
                revision=3,
                destination_epoch=4,
                active=True,
            )
        )
        session.add(
            Run(
                id="run-1",
                blog_id="blog-1",
                editorial_date=date.today(),
                quota_slot_key="fixture-slot-1",
                target_at=datetime.now(timezone.utc) + timedelta(hours=1),
                execution_mode="sandbox",
                config_snapshot={
                    "blog_id": "blog-1",
                    "provider_id": "provider-1",
                    "blog_revision": 3,
                    "destination_epoch": 4,
                    "provider_revision": 2,
                },
                status="queued",
                stage="queued",
            )
        )

    monkeypatch.setattr(pipeline_module, "OpenAICompatibleProvider", lambda config: FakeProvider(config, state))
    monkeypatch.setattr(pipeline_module, "BlogApiAdapter", lambda **kwargs: FakeBlogAdapter(state=state, **kwargs))
    monkeypatch.setattr(pipeline_module, "TrendCollector", lambda **_kwargs: FakeTrendCollector(state))
    monkeypatch.setattr(pipeline_module, "LiteratureSources", lambda **kwargs: FakeLiteratureSources(state=state, **kwargs))
    monkeypatch.setattr(pipeline_module, "SourceRetriever", FakeRetriever)

    original_prepare = DeliveryService.prepare

    def crash_before_outbox_once(service, session, *, run_id):
        if state.crash_before_outbox_once:
            state.crash_before_outbox_once = False
            raise SimulatedWorkerCrash("worker stopped after the article transaction committed")
        return original_prepare(service, session, run_id=run_id)

    monkeypatch.setattr(DeliveryService, "prepare", crash_before_outbox_once)

    def worker(worker_id: str) -> RunWorker:
        processor = pipeline_module.DailyResearchProcessor(sessions, settings)
        return RunWorker(sessions, worker_id=worker_id, processor=processor, concurrency=1)

    def expire_lease():
        with sessions.begin() as session:
            session.get(Run, "run-1").lease_until = datetime.now(timezone.utc) - timedelta(seconds=1)

    with pytest.raises(SimulatedWorkerCrash, match="trend persistence"):
        await worker("worker-1").run_once()
    with sessions() as session:
        assert session.get(Run, "run-1").status == "running"
        assert session.scalar(select(func.count()).select_from(TrendObservation)) == 1
        assert session.scalar(select(func.count()).select_from(ArticlePairRecord)) == 0

    expire_lease()
    with pytest.raises(SimulatedWorkerCrash, match="article transaction"):
        await worker("worker-2").run_once()
    with sessions() as session:
        assert session.get(Run, "run-1").status == "running"
        assert session.scalar(select(func.count()).select_from(TrendObservation)) == 1
        assert session.scalar(select(func.count()).select_from(ArticlePairRecord)) == 1
        assert session.scalar(select(func.count()).select_from(ArticleVersion)) == 2
        assert session.scalar(select(func.count()).select_from(DeliveryIntent)) == 0

    expire_lease()
    assert await worker("worker-3").run_once() is True

    with sessions() as session:
        run = session.get(Run, "run-1")
        assert run.status == "completed" and run.stage == "completed"
        assert run.editorial_decision == "draft"
        assert session.scalar(select(func.count()).select_from(TrendObservation)) == 1
        assert session.scalar(select(func.count()).select_from(ArticlePairRecord)) == 1
        assert session.scalar(select(func.count()).select_from(ArticleVersion)) == 2
        assert session.scalar(select(func.count()).select_from(SourceRecord)) == 1
        assert session.scalar(select(func.count()).select_from(EvidenceSpanRecord)) == 3
        assert session.scalar(select(func.count()).select_from(ContentLedger)) == 2
        pair = session.scalar(select(ArticlePairRecord).where(ArticlePairRecord.run_id == run.id))
        agent_trace = pair.review_result["agent"]["trace"]
        assert pair.review_result["agent"]["physical_requests"] == 11
        assert pair.review_result["agent"]["research_physical_requests"] == 9
        assert pair.review_result["agent"]["editorial_physical_requests"] == 2
        assert pair.review_result["agent"]["tool_actions"] == 8
        assert {item.get("name") for item in agent_trace if item.get("type") == "tool"} >= {
            "literature.search", "sources.read", "history.find_related", "research.finish"
        }
        intents = session.scalars(select(DeliveryIntent).order_by(DeliveryIntent.unit_key)).all()
        assert len(intents) == 2
        assert {intent.state for intent in intents} == {"prepared"}
        assert session.scalar(select(func.count()).select_from(DeliveryAttempt)) == 0
        events = session.scalars(select(RunEvent).where(RunEvent.run_id == run.id)).all()
        assert sum(event.event_type == "article_pair_saved" for event in events) == 1
        assert sum(event.event_type == "delivery_prepared" for event in events) == 1
        coverage_events = [event for event in events if event.event_type == "discovery_coverage"]
        assert len(coverage_events) == 1
        assert coverage_events[0].details["coverage"]["sources"]["fixture"]["successful"] == 1

    assert state.trend_collections == 2
    assert state.blog_history_reads == 1
    assert state.literature_searches == 1
    assert state.source_reads == 1
    assert state.editorial_checks == 1
    assert state.provider_requests == 12
    engine.dispose()
