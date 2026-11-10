import asyncio
from types import SimpleNamespace

from sqlalchemy import select

from takumi_research_agent.persistence.database import create_database
from takumi_research_agent.persistence.models import (
    Base,
    BlogHistoryEmbedding,
    BlogProfile,
)
from takumi_research_agent.research.semantic_history import SemanticHistoryRetriever


class FakeEmbeddings:
    def __init__(self):
        self.calls = 0

    async def create(self, *, model, input, encoding_format):
        self.calls += 1
        rows = []
        for index, text in enumerate(input):
            normalized = text.casefold()
            related = any(term in normalized for term in ("quantum", "qubit", "kuantum"))
            rows.append(SimpleNamespace(index=index, embedding=[1.0, 0.0] if related else [0.0, 1.0]))
        return SimpleNamespace(data=rows)


class FakeClient:
    def __init__(self):
        self.embeddings = FakeEmbeddings()

    async def close(self):
        return None


def _history():
    return [
        {"id": "en", "title": "Quantum computing and qubits", "language": "en"},
        {"id": "id", "title": "Komputasi kuantum dan qubit", "language": "id"},
        {"id": "noise", "title": "Bread and baking", "language": "en"},
    ]


def _create_retriever(sessions, client):
    return SemanticHistoryRetriever(
        sessions=sessions,
        blog_id="blog-1",
        model="multilingual-test-model",
        api_key="test-key",
        base_url="https://embeddings.example/v1",
        timeout_seconds=10,
        min_similarity=0.75,
        client=client,
    )


def test_semantic_history_cache_is_persisted_per_blog_model_and_content():
    engine, sessions = create_database("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with sessions.begin() as session:
        session.add(BlogProfile(
            id="blog-1",
            name="Test blog",
            base_url="https://blog.example/api",
            encrypted_api_token="",
            adapter_mapping={},
            capabilities={},
            capability_result={},
            topic_settings={},
            editorial_settings={},
        ))

    first_client = FakeClient()
    first = _create_retriever(sessions, first_client)
    asyncio.run(first.prepare(_history()))
    results = asyncio.run(first.search("Komputasi kuantum dan qubit"))
    assert [row["id"] for row in results] == ["en", "id"]
    assert first_client.embeddings.calls == 2  # one corpus batch and one query
    asyncio.run(first.close())

    second_client = FakeClient()
    second = _create_retriever(sessions, second_client)
    asyncio.run(second.prepare(_history()))
    assert second_client.embeddings.calls == 0
    assert len(second.vectors) == 3
    with sessions() as session:
        rows = session.scalars(select(BlogHistoryEmbedding)).all()
    assert len(rows) == 3
    asyncio.run(second.close())
    engine.dispose()
