from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from takumi_research_agent.blogs.adapter import HistoryBatch, HistoryCheckpoint
from takumi_research_agent.orchestration.pipeline import DailyResearchProcessor
from takumi_research_agent.persistence.database import create_database
from takumi_research_agent.persistence.models import Base, BlogHistory, BlogHistorySyncState, BlogProfile


class InterruptedAdapter:
    history_mapping_hash = "f" * 64

    async def read_history_batch(self, *, checkpoint=None, max_pages=1):
        if checkpoint is None:
            return HistoryBatch(
                records=({"id": "remote-1", "title": "First post", "language": "en", "content": "First body"},),
                checkpoint=HistoryCheckpoint(
                    pagination_mode="cursor",
                    mapping_hash=self.history_mapping_hash,
                    next_cursor="private-cursor-value",
                    pages_completed=1,
                    records_seen=1,
                    seen_cursors=("private-cursor-value",),
                ),
                complete=False,
                pages_read=1,
            )
        raise RuntimeError("simulated worker interruption")


class ResumedAdapter:
    history_mapping_hash = "f" * 64

    def __init__(self):
        self.received_checkpoint = None

    async def read_history_batch(self, *, checkpoint=None, max_pages=1):
        self.received_checkpoint = checkpoint
        return HistoryBatch(
            records=({"id": "remote-2", "title": "Second post", "language": "id", "content": "Second body"},),
            checkpoint=None,
            complete=True,
            pages_read=1,
        )


class DeltaAdapter:
    history_mapping_hash = "f" * 64
    mapping = SimpleNamespace(delta_since_parameter="updated_after", delta_overlap_seconds=90)

    def __init__(self):
        self.modified_since = None

    async def read_history_batch(self, *, checkpoint=None, max_pages=1, modified_since=None):
        self.modified_since = modified_since
        return HistoryBatch(
            records=(
                {"id": "remote-1", "title": "Updated title", "language": "en", "content": "Updated body"},
                {"id": "remote-2", "title": "New delta post", "language": "en", "content": "New body"},
            ),
            checkpoint=None,
            complete=True,
            pages_read=1,
        )


def test_history_batch_and_checkpoint_survive_worker_restart_without_early_pruning():
    engine, sessions = create_database("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with sessions.begin() as session:
        session.add(BlogProfile(id="blog-1", name="Test blog", base_url="https://blog.example"))
        session.add(
            BlogHistory(
                id="old-row",
                blog_id="blog-1",
                remote_id="old-remote",
                language="en",
                title="Previous snapshot post",
                canonical_record={"id": "old-remote", "title": "Previous snapshot post"},
                snapshot_id="previous-snapshot",
            )
        )
    processor = DailyResearchProcessor.__new__(DailyResearchProcessor)
    processor.sessions = sessions
    processor.settings = SimpleNamespace(master_key="history-sync-test-master-key-0001")

    with pytest.raises(RuntimeError, match="interruption"):
        asyncio.run(processor._sync_history_resumable("blog-1", "run-1", InterruptedAdapter()))

    with sessions() as session:
        state = session.get(BlogHistorySyncState, "blog-1")
        assert state is not None and not state.complete
        assert state.checkpoint is not None
        assert "private-cursor-value" not in json.dumps(state.checkpoint)
        assert {row.remote_id for row in session.query(BlogHistory).all()} == {"old-remote", "remote-1"}

    resumed_adapter = ResumedAdapter()
    history = asyncio.run(processor._sync_history_resumable("blog-1", "run-1", resumed_adapter))
    assert {item["id"] for item in history} == {"remote-1", "remote-2"}
    assert resumed_adapter.received_checkpoint["next_cursor"] == "private-cursor-value"

    with sessions() as session:
        state = session.get(BlogHistorySyncState, "blog-1")
        assert state is not None and state.complete
        assert state.checkpoint is None
        assert state.records_synced == 2
        assert {row.remote_id for row in session.query(BlogHistory).all()} == {"remote-1", "remote-2"}
    engine.dispose()


def test_delta_uses_overlap_watermark_and_preserves_unseen_history():
    engine, sessions = create_database("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    watermark = datetime(2026, 11, 10, 4, 0, tzinfo=timezone.utc)
    with sessions.begin() as session:
        session.add(BlogProfile(id="blog-1", name="Test blog", base_url="https://blog.example"))
        session.add(
            BlogHistorySyncState(
                blog_id="blog-1",
                sync_run_id="run-old",
                mapping_hash="f" * 64,
                snapshot_id="snapshot-old",
                sync_mode="full",
                committed_watermark=watermark,
                checkpoint=None,
                complete=True,
                records_synced=2,
                started_at=watermark,
                completed_at=watermark,
            )
        )
        session.add_all(
            [
                BlogHistory(
                    id="remote-1-row",
                    blog_id="blog-1",
                    remote_id="remote-1",
                    language="en",
                    title="Previous title",
                    canonical_record={"id": "remote-1", "title": "Previous title"},
                    snapshot_id="snapshot-old",
                ),
                BlogHistory(
                    id="stale-row",
                    blog_id="blog-1",
                    remote_id="not-in-delta",
                    language="en",
                    title="Still in history",
                    canonical_record={"id": "not-in-delta", "title": "Still in history"},
                    snapshot_id="snapshot-old",
                ),
            ]
        )
    processor = DailyResearchProcessor.__new__(DailyResearchProcessor)
    processor.sessions = sessions
    processor.settings = SimpleNamespace(master_key="history-sync-test-master-key-0001")
    adapter = DeltaAdapter()

    history = asyncio.run(processor._sync_history_resumable("blog-1", "run-new", adapter))

    assert adapter.modified_since == watermark - timedelta(seconds=90)
    assert {row["id"] for row in history} == {"remote-1", "remote-2", "not-in-delta"}
    assert next(row for row in history if row["id"] == "remote-1")["title"] == "Updated title"
    with sessions() as session:
        state = session.get(BlogHistorySyncState, "blog-1")
        assert state is not None and state.complete and state.sync_mode == "delta"
        assert state.committed_watermark is not None
        assert state.committed_watermark.replace(tzinfo=timezone.utc) > watermark
        assert {row.remote_id for row in session.query(BlogHistory).all()} == {"remote-1", "remote-2", "not-in-delta"}
    engine.dispose()
