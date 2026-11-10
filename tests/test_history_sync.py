from __future__ import annotations

import asyncio
import json
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
