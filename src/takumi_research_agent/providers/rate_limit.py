from __future__ import annotations

import asyncio
import hashlib
import time
from typing import Any

from sqlalchemy import case, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from takumi_research_agent.persistence.models import ProviderRateGate
from takumi_research_agent.providers.base import ModelMessage, ModelReply


def provider_quota_key(provider_type: str, base_url: str | None, api_key: str) -> str:
    """Hash endpoint and credential so equal credentials share one quota without storing the key."""
    canonical = f"{provider_type}\n{(base_url or '').rstrip('/').casefold()}\n{api_key}"
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def reserve_provider_slot(sessions, *, key_hash: str, requests_per_minute: int, now: float | None = None) -> float:
    """Atomically reserve one request start time across all workers using the same database."""
    if not key_hash or len(key_hash) != 64:
        raise ValueError("key_hash must be a SHA-256 hex digest")
    if not 1 <= requests_per_minute <= 100_000:
        raise ValueError("requests_per_minute must be between 1 and 100000")
    current = time.time() if now is None else now
    interval = 60.0 / requests_per_minute

    with sessions.begin() as session:
        dialect = session.get_bind().dialect.name
        if dialect == "sqlite":
            insert = sqlite_insert(ProviderRateGate)
        elif dialect == "postgresql":
            insert = postgresql_insert(ProviderRateGate)
        else:
            raise RuntimeError(f"shared provider rate limiting is unsupported for database dialect {dialect}")
        session.execute(
            insert.values(key_hash=key_hash, next_allowed_at=0).on_conflict_do_nothing(
                index_elements=[ProviderRateGate.key_hash]
            )
        )
        next_allowed = case(
            (ProviderRateGate.next_allowed_at < current, current),
            else_=ProviderRateGate.next_allowed_at,
        ) + interval
        reserved_end = session.scalar(
            update(ProviderRateGate)
            .where(ProviderRateGate.key_hash == key_hash)
            .values(next_allowed_at=next_allowed)
            .returning(ProviderRateGate.next_allowed_at)
        )
        if reserved_end is None:
            raise RuntimeError("provider rate slot could not be reserved")
    return max(0.0, float(reserved_end) - interval - current)


class SharedRateLimitedProvider:
    """Decorate a model provider with a database-backed shared requests-per-minute gate."""

    def __init__(self, delegate, *, sessions, key_hash: str, requests_per_minute: int):
        self.delegate = delegate
        self.config = getattr(delegate, "config", None)
        self.sessions = sessions
        self.key_hash = key_hash
        self.requests_per_minute = requests_per_minute

    async def close(self) -> None:
        await self.delegate.close()

    async def complete(
        self,
        messages: list[ModelMessage],
        tools: list[dict[str, Any]],
        *,
        max_output_tokens: int,
        temperature: float = 0,
    ) -> ModelReply:
        await _wait_for_provider_slot(self.sessions, self.key_hash, self.requests_per_minute)
        return await self.delegate.complete(
            messages,
            tools,
            max_output_tokens=max_output_tokens,
            temperature=temperature,
        )


async def _wait_for_provider_slot(sessions, key_hash: str, requests_per_minute: int) -> None:
    delay = await asyncio.to_thread(
        reserve_provider_slot,
        sessions,
        key_hash=key_hash,
        requests_per_minute=requests_per_minute,
    )
    if delay:
        await asyncio.sleep(delay)


class SharedRateLimitedEmbeddingClient:
    """Rate-limit embedding requests through the same credential-scoped database gate."""

    class _Embeddings:
        def __init__(self, delegate, *, sessions, key_hash: str, requests_per_minute: int):
            self.delegate = delegate
            self.sessions = sessions
            self.key_hash = key_hash
            self.requests_per_minute = requests_per_minute

        async def create(self, **kwargs):
            await _wait_for_provider_slot(self.sessions, self.key_hash, self.requests_per_minute)
            return await self.delegate.create(**kwargs)

    def __init__(self, delegate, *, sessions, key_hash: str, requests_per_minute: int):
        self.delegate = delegate
        self.embeddings = self._Embeddings(
            delegate.embeddings,
            sessions=sessions,
            key_hash=key_hash,
            requests_per_minute=requests_per_minute,
        )

    async def close(self) -> None:
        await self.delegate.close()
