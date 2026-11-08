from __future__ import annotations

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
        url = self._url(self.list_operation.path)
        results: list[dict[str, Any]] = []
        seen_cursors: set[str] = set()
        seen_record_ids: set[tuple[str, str]] = set()
        params: dict[str, Any] = {}
        page_value = 1
        offset_value = 0
        for page_index in range(self.mapping.page_limit):
            if self.mapping.pagination_mode == "page":
                if not self.mapping.page_request_parameter:
                    raise ContractError("Page pagination requires a request parameter")
                params[self.mapping.page_request_parameter] = page_value
                if self.mapping.page_size_parameter:
                    params[self.mapping.page_size_parameter] = self.mapping.page_size
            elif self.mapping.pagination_mode == "offset":
                if not self.mapping.page_request_parameter:
                    raise ContractError("Offset pagination requires an offset request parameter")
                params[self.mapping.page_request_parameter] = offset_value
                if self.mapping.page_size_parameter:
                    params[self.mapping.page_size_parameter] = self.mapping.page_size
            if self.mapping.pagination_mode == "cursor" and self.mapping.page_size_parameter:
                params[self.mapping.page_size_parameter] = self.mapping.page_size
            payload = await self._read_history_page(url, params)
            records = normalize_records(payload, self.mapping)
            fresh_records = []
            for record in records:
                record_id = (str(record["id"]), str(record.get("language", "")))
                if record_id not in seen_record_ids:
                    fresh_records.append(record)
                    seen_record_ids.add(record_id)
            if page_index > 0 and records and not fresh_records:
                raise ContractError("Blog API pagination made no progress; history is incomplete")
            if len(results) + len(fresh_records) > 20_000:
                raise ContractError("Blog history exceeds the configured 20,000-record safety limit")
            for record in fresh_records:
                if "title" in record:
                    record["title"] = str(record["title"])[:1_000]
                if "content" in record:
                    record["content"] = str(record["content"])[:2_500]
                if "summary" in record:
                    record["summary"] = str(record["summary"])[:2_500]
            results.extend(fresh_records)
            if self.mapping.pagination_mode == "none":
                if has_unmapped_pagination_hint(payload, self.mapping.list_path):
                    raise ContractError("Response exposes a pagination hint; configure its cursor/page mapping before treating history as complete")
                return results
            if self.mapping.pagination_mode == "cursor":
                if not self.mapping.page_next_path or not self.mapping.page_request_parameter:
                    raise ContractError("Cursor pagination requires next path and request parameter")
                next_value = get_path(payload, self.mapping.page_next_path)
                if next_value in (None, "", False):
                    return results
                cursor = str(next_value)
                if cursor in seen_cursors:
                    raise ContractError("Blog API repeated a pagination cursor")
                seen_cursors.add(cursor)
                params[self.mapping.page_request_parameter] = cursor
            elif self.mapping.pagination_mode == "page":
                next_value = get_path(payload, self.mapping.page_next_path) if self.mapping.page_next_path else None
                if self.mapping.page_next_path and next_value in (None, "", False):
                    return results
                if not self.mapping.page_next_path and len(records) < self.mapping.page_size:
                    return results
                page_value += 1
            elif self.mapping.pagination_mode == "offset":
                if len(records) < self.mapping.page_size:
                    return results
                offset_value += len(records)
        raise ContractError("Configured pagination limit reached; history is incomplete")

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
