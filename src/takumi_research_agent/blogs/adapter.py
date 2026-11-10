from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import urljoin, urlsplit

import httpx

from .contracts import BlogMapping, ContractError, Operation, build_create_payload, get_path, has_unmapped_pagination_hint, normalize_records


class DeliveryOutcomeUnknown(RuntimeError):
    """The server may have committed a mutation; reconcile before retrying."""


@dataclass(frozen=True, slots=True)
class BlogCapabilities:
    list_posts: bool
    get_post: bool = False
    create_draft: bool = False
    publish: bool = False
    publish_existing: bool = False
    idempotency_key: bool = False
    lookup_by_external_key: bool = False
    pagination: Literal["none", "cursor", "offset", "page"] = "none"


@dataclass(frozen=True, slots=True)
class HistoryCheckpoint:
    """Opaque continuation state for a bounded history read; store it privately and do not log it."""

    pagination_mode: Literal["none", "cursor", "offset", "page"]
    mapping_hash: str
    next_cursor: str | None = None
    next_page: int = 1
    next_offset: int = 0
    pages_completed: int = 0
    records_seen: int = 0
    seen_cursors: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": 1,
            "pagination_mode": self.pagination_mode,
            "mapping_hash": self.mapping_hash,
            "next_cursor": self.next_cursor,
            "next_page": self.next_page,
            "next_offset": self.next_offset,
            "pages_completed": self.pages_completed,
            "records_seen": self.records_seen,
            "seen_cursors": list(self.seen_cursors),
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any], *, mapping: BlogMapping, mapping_hash: str) -> "HistoryCheckpoint":
        if not isinstance(value, dict) or value.get("version") != 1:
            raise ContractError("History checkpoint version is invalid")
        if value.get("pagination_mode") != mapping.pagination_mode or value.get("mapping_hash") != mapping_hash:
            raise ContractError("History checkpoint does not match the active blog mapping")
        if any(type(value.get(name, default)) is not int for name, default in (("next_page", 1), ("next_offset", 0), ("pages_completed", 0), ("records_seen", 0))):
            raise ContractError("History checkpoint counters must be integers")
        next_page = value.get("next_page", 1)
        next_offset = value.get("next_offset", 0)
        pages_completed = value.get("pages_completed", 0)
        records_seen = value.get("records_seen", 0)
        if not 1 <= next_page <= mapping.page_limit or not 0 <= next_offset <= 20_000 or not 0 <= pages_completed < mapping.page_limit or not 0 <= records_seen <= 20_000:
            raise ContractError("History checkpoint exceeds configured page or record limits")
        next_cursor = value.get("next_cursor")
        if next_cursor is not None and (not isinstance(next_cursor, str) or not next_cursor or len(next_cursor) > 2_000):
            raise ContractError("History checkpoint cursor is invalid")
        raw_seen = value.get("seen_cursors", [])
        if not isinstance(raw_seen, list) or len(raw_seen) > mapping.page_limit or any(not isinstance(item, str) or not item or len(item) > 2_000 for item in raw_seen):
            raise ContractError("History checkpoint cursor history is invalid")
        seen_cursors = tuple(raw_seen)
        if len(set(seen_cursors)) != len(seen_cursors) or (next_cursor is not None and next_cursor not in seen_cursors):
            raise ContractError("History checkpoint cursor history is inconsistent")
        return cls(
            pagination_mode=mapping.pagination_mode,
            mapping_hash=mapping_hash,
            next_cursor=next_cursor,
            next_page=next_page,
            next_offset=next_offset,
            pages_completed=pages_completed,
            records_seen=records_seen,
            seen_cursors=seen_cursors,
        )


@dataclass(frozen=True, slots=True)
class HistoryBatch:
    records: tuple[dict[str, Any], ...]
    checkpoint: HistoryCheckpoint | None
    complete: bool
    pages_read: int


class BlogApiAdapter:
    def __init__(
        self,
        *,
        base_url: str,
        token: str,
        mapping: BlogMapping,
        list_operation: Operation,
        create_operation: Operation | None = None,
        auth_header: str = "Authorization",
        auth_scheme: str = "Bearer",
        capabilities: BlogCapabilities | None = None,
        allow_insecure_private: bool = False,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        parsed = urlsplit(base_url)
        if parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username or parsed.password:
            raise ContractError("Blog base URL must be an absolute HTTP(S) URL without embedded credentials")
        if parsed.scheme != "https" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"} and not allow_insecure_private:
            raise ContractError("Non-local blog endpoints must use HTTPS")
        if list_operation.method != "get":
            raise ContractError("History list operation must use GET")
        if create_operation and create_operation.method not in {"post", "put"}:
            raise ContractError("Create operation must use POST or PUT")
        self.base_url = base_url.rstrip("/") + "/"
        self.token = token
        self.mapping = mapping
        self.list_operation = list_operation
        self.create_operation = create_operation
        self.capabilities = capabilities or BlogCapabilities(list_posts=True, create_draft=create_operation is not None)
        headers = {auth_header: f"{auth_scheme} {token}".strip()} if token else {}
        self.client = httpx.AsyncClient(timeout=httpx.Timeout(30, connect=10), follow_redirects=False, headers=headers, transport=transport)

    async def _read_history_page(self, url: str, params: dict[str, Any], *, max_bytes: int = 5_000_000) -> Any:
        async with self.client.stream("GET", url, params=params) as response:
            response.raise_for_status()
            content_length = response.headers.get("content-length")
            if content_length:
                try:
                    declared_bytes = int(content_length)
                except ValueError as exc:
                    raise ContractError("Blog history response has an invalid Content-Length") from exc
                if declared_bytes > max_bytes:
                    raise ContractError("Blog history response exceeds the configured 5 MB page limit")
            body = bytearray()
            async for chunk in response.aiter_bytes():
                if len(body) + len(chunk) > max_bytes:
                    raise ContractError("Blog history response exceeds the configured 5 MB page limit")
                body.extend(chunk)
        try:
            return json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ContractError("Blog history endpoint did not return valid JSON") from exc

    async def close(self) -> None:
        await self.client.aclose()

    def _url(self, path: str) -> str:
        if path.startswith("http://") or path.startswith("https://"):
            raise ContractError("Operation path must be relative to the configured blog origin")
        url = urljoin(self.base_url, path.lstrip("/"))
        if urlsplit(url).netloc != urlsplit(self.base_url).netloc:
            raise ContractError("Operation path escaped the configured blog origin")
        return url

    async def list_posts(self) -> list[dict[str, Any]]:
        if not self.capabilities.list_posts:
            raise ContractError("Blog adapter has not passed the list capability check")
        results: list[dict[str, Any]] = []
        seen_record_ids: set[tuple[str, str]] = set()
        checkpoint: HistoryCheckpoint | None = None
        while True:
            batch = await self.read_history_batch(checkpoint=checkpoint, max_pages=self.mapping.page_limit)
            for record in batch.records:
                record_id = (str(record["id"]), str(record.get("language", "")))
                if record_id in seen_record_ids:
                    continue
                seen_record_ids.add(record_id)
                results.append(record)
                if len(results) > 20_000:
                    raise ContractError("Blog history exceeds the configured 20,000-record safety limit")
            if batch.complete:
                return results
            if batch.checkpoint is None:
                raise ContractError("History pagination stopped without a continuation checkpoint")
            checkpoint = batch.checkpoint

    def _history_mapping_hash(self) -> str:
        mapping_json = json.dumps(self.mapping.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(mapping_json.encode("utf-8")).hexdigest()

    async def read_history_batch(
        self,
        *,
        checkpoint: HistoryCheckpoint | dict[str, Any] | None = None,
        max_pages: int = 1,
    ) -> HistoryBatch:
        """Read bounded pages; the private checkpoint lets a caller resume after persisting a batch."""
        if not self.capabilities.list_posts:
            raise ContractError("Blog adapter has not passed the list capability check")
        if type(max_pages) is not int or not 1 <= max_pages <= self.mapping.page_limit:
            raise ContractError("History batch page count is outside the configured limit")
        mapping_hash = self._history_mapping_hash()
        if checkpoint is None:
            state = HistoryCheckpoint(self.mapping.pagination_mode, mapping_hash)
        elif isinstance(checkpoint, HistoryCheckpoint):
            state = HistoryCheckpoint.from_dict(checkpoint.to_dict(), mapping=self.mapping, mapping_hash=mapping_hash)
        else:
            state = HistoryCheckpoint.from_dict(checkpoint, mapping=self.mapping, mapping_hash=mapping_hash)
        if state.pages_completed >= self.mapping.page_limit:
            raise ContractError("Configured pagination limit reached; history is incomplete")

        url = self._url(self.list_operation.path)
        results: list[dict[str, Any]] = []
        seen_record_ids: set[tuple[str, str]] = set()
        seen_cursors = set(state.seen_cursors)
        current = state
        pages_read = 0
        for _ in range(max_pages):
            if current.pages_completed >= self.mapping.page_limit:
                raise ContractError("Configured pagination limit reached; history is incomplete")
            params: dict[str, Any] = {}
            if self.mapping.pagination_mode == "page":
                if not self.mapping.page_request_parameter:
                    raise ContractError("Page pagination requires a request parameter")
                params[self.mapping.page_request_parameter] = current.next_page
                if self.mapping.page_size_parameter:
                    params[self.mapping.page_size_parameter] = self.mapping.page_size
            elif self.mapping.pagination_mode == "offset":
                if not self.mapping.page_request_parameter:
                    raise ContractError("Offset pagination requires a request parameter")
                params[self.mapping.page_request_parameter] = current.next_offset
                if self.mapping.page_size_parameter:
                    params[self.mapping.page_size_parameter] = self.mapping.page_size
            if self.mapping.pagination_mode == "cursor" and self.mapping.page_size_parameter:
                params[self.mapping.page_size_parameter] = self.mapping.page_size
            if self.mapping.pagination_mode == "cursor" and current.next_cursor:
                if not self.mapping.page_request_parameter:
                    raise ContractError("Cursor pagination requires a request parameter")
                params[self.mapping.page_request_parameter] = current.next_cursor

            payload = await self._read_history_page(url, params)
            records = normalize_records(payload, self.mapping)
            fresh_records: list[dict[str, Any]] = []
            for record in records:
                record_id = (str(record["id"]), str(record.get("language", "")))
                if record_id not in seen_record_ids:
                    fresh_records.append(record)
                    seen_record_ids.add(record_id)
            if current.pages_completed + pages_read > 0 and records and not fresh_records:
                raise ContractError("Blog API pagination made no progress; history is incomplete")
            if current.records_seen + len(fresh_records) > 20_000:
                raise ContractError("Blog history exceeds the configured 20,000-record safety limit")
            for record in fresh_records:
                if "title" in record:
                    record["title"] = str(record["title"])[:1_000]
                if "content" in record:
                    record["content"] = str(record["content"])[:2_500]
                if "summary" in record:
                    record["summary"] = str(record["summary"])[:2_500]
            results.extend(fresh_records)
            pages_read += 1
            pages_completed = current.pages_completed + 1

            if self.mapping.pagination_mode == "none":
                if has_unmapped_pagination_hint(payload, self.mapping.list_path):
                    raise ContractError("Response exposes a pagination hint; configure its pagination mapping before treating history as complete")
                return HistoryBatch(tuple(results), None, True, pages_read)
            if self.mapping.pagination_mode == "cursor":
                if not self.mapping.page_next_path or not self.mapping.page_request_parameter:
                    raise ContractError("Cursor pagination requires next path and request parameter")
                next_value = get_path(payload, self.mapping.page_next_path)
                if next_value is None or next_value is False or next_value == "":
                    return HistoryBatch(tuple(results), None, True, pages_read)
                cursor = str(next_value)
                if cursor in seen_cursors:
                    raise ContractError("Blog API repeated a pagination cursor")
                seen_cursors.add(cursor)
                next_state = HistoryCheckpoint(
                    pagination_mode=self.mapping.pagination_mode,
                    mapping_hash=mapping_hash,
                    next_cursor=cursor,
                    next_page=current.next_page,
                    next_offset=current.next_offset,
                    pages_completed=pages_completed,
                    records_seen=current.records_seen + len(fresh_records),
                    seen_cursors=tuple(sorted(seen_cursors)),
                )
            elif self.mapping.pagination_mode == "page":
                next_value = get_path(payload, self.mapping.page_next_path) if self.mapping.page_next_path else None
                if self.mapping.page_next_path and (next_value is None or next_value is False or next_value == ""):
                    return HistoryBatch(tuple(results), None, True, pages_read)
                if not self.mapping.page_next_path and len(records) < self.mapping.page_size:
                    return HistoryBatch(tuple(results), None, True, pages_read)
                next_state = HistoryCheckpoint(
                    pagination_mode=self.mapping.pagination_mode,
                    mapping_hash=mapping_hash,
                    next_page=current.next_page + 1,
                    next_offset=current.next_offset,
                    pages_completed=pages_completed,
                    records_seen=current.records_seen + len(fresh_records),
                    seen_cursors=current.seen_cursors,
                )
            else:
                if len(records) < self.mapping.page_size:
                    return HistoryBatch(tuple(results), None, True, pages_read)
                next_state = HistoryCheckpoint(
                    pagination_mode=self.mapping.pagination_mode,
                    mapping_hash=mapping_hash,
                    next_page=current.next_page,
                    next_offset=current.next_offset + len(records),
                    pages_completed=pages_completed,
                    records_seen=current.records_seen + len(fresh_records),
                    seen_cursors=current.seen_cursors,
                )
            if pages_completed >= self.mapping.page_limit:
                raise ContractError("Configured pagination limit reached; history is incomplete")
            current = next_state
            if pages_read >= max_pages:
                return HistoryBatch(tuple(results), current, False, pages_read)
        raise ContractError("History batch ended without a completion status")

    async def create(self, canonical: dict[str, Any], *, intent_key: str, publish: bool) -> dict[str, Any]:
        payload = build_create_payload(canonical, self.mapping)
        return await self.send_payload(payload, intent_key=intent_key, publish=publish)

    async def send_payload(self, payload: dict[str, Any], *, intent_key: str, publish: bool) -> dict[str, Any]:
        """Send the exact stored payload so preview, outbox, and wire body cannot drift."""
        if not self.create_operation:
            raise ContractError("No create endpoint is configured")
        if publish and not self.capabilities.publish:
            raise ContractError("Publish capability has not been proven")
        if not publish and not self.capabilities.create_draft:
            raise ContractError("Draft capability has not been proven")
        headers = {"Idempotency-Key": intent_key} if self.capabilities.idempotency_key else {}
        try:
            response = await self.client.request(
                self.create_operation.method.upper(),
                self._url(self.create_operation.path),
                json=payload,
                headers=headers,
            )
        except httpx.TimeoutException as exc:
            raise DeliveryOutcomeUnknown("Blog create timed out; reconcile the same intent before retrying") from exc
        except httpx.TransportError as exc:
            raise DeliveryOutcomeUnknown("Blog create transport failed; remote outcome may be unknown") from exc
        if response.status_code >= 500:
            raise DeliveryOutcomeUnknown(f"Blog returned {response.status_code}; remote mutation outcome is unknown")
        response.raise_for_status()
        try:
            body = response.json()
        except ValueError:
            body = {"raw_text": response.text[:2_000]}
        return {"http_status": response.status_code, "body": body, "request_payload": payload, "intent_key": intent_key}
