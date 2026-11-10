from __future__ import annotations

import hashlib
from typing import Any

from openai import AsyncOpenAI
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from takumi_research_agent.persistence.models import BlogHistoryEmbedding
from takumi_research_agent.providers.embeddings import embed_texts
from takumi_research_agent.providers.rate_limit import (
    SharedRateLimitedEmbeddingClient,
    provider_quota_key,
)

MAX_HISTORY_EMBEDDING_TEXT_CHARS = 8_000
DB_QUERY_CHUNK_SIZE = 500


def _history_text(post: dict[str, Any]) -> str:
    return " ".join(
        str(post.get(field, ""))
        for field in ("title", "summary", "content")
        if post.get(field) is not None
    ).strip()[:MAX_HISTORY_EMBEDDING_TEXT_CHARS]


def _cosine(left: list[float], right: list[float]) -> float:
    if len(left) != len(right):
        raise ValueError("cached embedding dimensions do not match the query embedding")
    return sum(a * b for a, b in zip(left, right, strict=True))


class SemanticHistoryRetriever:
    """Database-cached semantic index for one complete blog history snapshot."""

    def __init__(
        self,
        *,
        sessions,
        blog_id: str,
        model: str,
        api_key: str,
        base_url: str | None,
        timeout_seconds: float,
        min_similarity: float,
        requests_per_minute: int = 0,
        client=None,
    ):
        if not 0 <= min_similarity <= 1:
            raise ValueError("min_similarity must be between 0 and 1")
        self.sessions = sessions
        self.blog_id = blog_id
        self.model = model
        self.cache_model = f"{(base_url or 'openai').rstrip('/')}::{model}"
        self.min_similarity = min_similarity
        client = client or AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=timeout_seconds,
            max_retries=0,
        )
        if requests_per_minute:
            client = SharedRateLimitedEmbeddingClient(
                client,
                sessions=sessions,
                key_hash=provider_quota_key("openai_compatible", base_url, api_key),
                requests_per_minute=requests_per_minute,
            )
        self.client = client
        self.records: list[dict[str, Any]] = []
        self.vectors: dict[str, list[float]] = {}
        self.query_cache: dict[str, list[float]] = {}

    async def close(self) -> None:
        await self.client.close()

    def _load_cached(self, digests: list[str]) -> dict[str, list[float]]:
        cached: dict[str, list[float]] = {}
        with self.sessions() as session:
            for start in range(0, len(digests), DB_QUERY_CHUNK_SIZE):
                chunk = digests[start : start + DB_QUERY_CHUNK_SIZE]
                rows = session.execute(
                    select(BlogHistoryEmbedding.content_hash, BlogHistoryEmbedding.vector).where(
                        BlogHistoryEmbedding.blog_id == self.blog_id,
                        BlogHistoryEmbedding.embedding_model == self.cache_model,
                        BlogHistoryEmbedding.content_hash.in_(chunk),
                    )
                )
                cached.update({digest: list(vector) for digest, vector in rows})
        return cached

    def _save_cached(self, rows: list[dict[str, Any]]) -> None:
        if not rows:
            return
        with self.sessions.begin() as session:
            dialect = session.get_bind().dialect.name
            if dialect == "sqlite":
                insert = sqlite_insert(BlogHistoryEmbedding)
            elif dialect == "postgresql":
                insert = postgresql_insert(BlogHistoryEmbedding)
            else:
                raise RuntimeError(f"semantic history cache is unsupported for database dialect {dialect}")
            session.execute(
                insert.values(rows).on_conflict_do_nothing(
                    index_elements=[
                        BlogHistoryEmbedding.blog_id,
                        BlogHistoryEmbedding.embedding_model,
                        BlogHistoryEmbedding.content_hash,
                    ]
                )
            )

    async def prepare(self, posts: list[dict[str, Any]]) -> None:
        records = []
        for post in posts:
            text = _history_text(post)
            if not text:
                continue
            digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
            records.append({
                "id": str(post.get("id", "")),
                "title": str(post.get("title", ""))[:300],
                "language": str(post.get("language", "")),
                "excerpt": text[:600],
                "content_hash": digest,
            })
        self.records = records
        if not records:
            self.vectors = {}
            return

        digests = sorted({record["content_hash"] for record in records})
        self.vectors = self._load_cached(digests)
        missing = [digest for digest in digests if digest not in self.vectors]
        texts_by_digest = {
            hashlib.sha256(_history_text(post).encode("utf-8")).hexdigest(): _history_text(post)
            for post in posts
            if _history_text(post)
        }
        cache_rows = []
        for start in range(0, len(missing), 64):
            batch_digests = missing[start : start + 64]
            batch_texts = [texts_by_digest[digest] for digest in batch_digests]
            batch_vectors = await embed_texts(self.client, batch_texts, model=self.model, batch_size=64)
            for digest, vector in zip(batch_digests, batch_vectors, strict=True):
                self.vectors[digest] = vector
                cache_rows.append({
                    "blog_id": self.blog_id,
                    "embedding_model": self.cache_model,
                    "content_hash": digest,
                    "vector": vector,
                    "dimensions": len(vector),
                })
        self._save_cached(cache_rows)

    async def search(self, topic: str, *, limit: int = 8) -> list[dict[str, Any]]:
        if not self.records:
            return []
        if not isinstance(topic, str) or not topic.strip():
            return []
        query = topic.strip()[:MAX_HISTORY_EMBEDDING_TEXT_CHARS]
        normalized_query = query.casefold()
        vector = self.query_cache.get(normalized_query)
        if vector is None:
            vector = (await embed_texts(self.client, [query], model=self.model, batch_size=1))[0]
            self.query_cache[normalized_query] = vector
        ranked = []
        for record in self.records:
            score = _cosine(vector, self.vectors[record["content_hash"]])
            if score >= self.min_similarity:
                ranked.append({**record, "similarity_hint": round(score, 4)})
        return sorted(ranked, key=lambda item: (-item["similarity_hint"], item["id"]))[: max(0, limit)]
