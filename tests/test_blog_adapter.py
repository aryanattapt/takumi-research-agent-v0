from __future__ import annotations

import json

import httpx
import pytest
from pydantic import ValidationError

from takumi_research_agent.blogs.adapter import BlogApiAdapter, BlogCapabilities, DeliveryOutcomeUnknown
from takumi_research_agent.blogs.contracts import BlogMapping, ContractError, infer_collection_path, infer_response_mapping, normalize_records


def mapping(**overrides):
    value = {
        "list_path": "$.data.items",
        "response_fields": {"id": "$.post.id", "title": "$.post.title", "content": "$.post.content", "status": "$.post.state"},
        "create_fields": {"title": "$.article.title", "content": "$.article.body", "locale": "$.article.language", "status": "$.article.state"},
        "publish_status_values": {"draft": "drafting", "publish": "live"},
    }
    value.update(overrides)
    return BlogMapping.model_validate(value)


def test_nested_response_and_payload_mapping_are_data_driven():
    sample = {"data": {"items": [{"post": {"id": "42", "title": "A study", "content": "Text", "state": "live"}}]}}
    normalized = normalize_records(sample, mapping())
    assert normalized == [{"id": "42", "title": "A study", "content": "Text", "status": "publish"}]
    adapter_mapping = mapping()
    from takumi_research_agent.blogs.contracts import build_create_payload
    payload = build_create_payload({"title": "Title", "content": "Body", "locale": "id", "status": "publish"}, adapter_mapping)
    assert payload == {"article": {"title": "Title", "body": "Body", "language": "id", "state": "live"}}


def test_ambiguous_sample_mapping_does_not_guess_collection():
    sample = {"data": [{"id": "1", "title": "A", "content": "one"}], "archive": [{"id": "2", "title": "B", "content": "two"}]}
    assert infer_collection_path(sample) is None
    assert infer_response_mapping(sample) == {}


def test_unsafe_or_executable_mapping_syntax_is_rejected():
    with pytest.raises(ValidationError):
        BlogMapping.model_validate({
            "list_path": "$..items",
            "response_fields": {"id": "$.id", "title": "$.title"},
            "create_fields": {"title": "$.title", "content": "$.body"},
        })


@pytest.mark.asyncio
async def test_list_pagination_detects_repeated_cursor():
    def handler(request: httpx.Request):
        return httpx.Response(200, json={"data": {"items": [{"post": {"id": "1", "title": "Title", "content": "Body"}}], "next": "same"}})

    adapter = BlogApiAdapter(
        base_url="https://blog.example/api",
        token="secret",
        mapping=mapping(pagination_mode="cursor", page_next_path="$.data.next", page_request_parameter="cursor", page_limit=5),
        list_operation=type("Op", (), {"method": "get", "path": "/posts"})(),
        capabilities=BlogCapabilities(list_posts=True),
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(ContractError, match="repeated|no progress"):
            await adapter.list_posts()
    finally:
        await adapter.close()


@pytest.mark.asyncio
async def test_create_timeout_becomes_unknown_and_keeps_same_intent_key():
    def handler(_request: httpx.Request):
        raise httpx.ReadTimeout("timeout")

    adapter = BlogApiAdapter(
        base_url="https://blog.example/api",
        token="secret",
        mapping=mapping(),
        list_operation=type("Op", (), {"method": "get", "path": "/posts"})(),
        create_operation=type("Op", (), {"method": "post", "path": "/posts"})(),
        capabilities=BlogCapabilities(list_posts=True, create_draft=True, idempotency_key=True),
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(DeliveryOutcomeUnknown, match="reconcile"):
            await adapter.create({"title": "Title", "content": "Body", "locale": "en", "status": "draft"}, intent_key="stable-key", publish=False)
    finally:
        await adapter.close()


@pytest.mark.asyncio
async def test_send_payload_posts_the_exact_stored_payload():
    seen: dict[str, object] = {}

    def handler(request: httpx.Request):
        seen["payload"] = json.loads(request.content)
        seen["idempotency_key"] = request.headers.get("Idempotency-Key")
        return httpx.Response(201, json={"post": {"id": "9"}})

    adapter = BlogApiAdapter(
        base_url="https://blog.example/api",
        token="secret",
        mapping=mapping(),
        list_operation=type("Op", (), {"method": "get", "path": "/posts"})(),
        create_operation=type("Op", (), {"method": "post", "path": "/posts"})(),
        capabilities=BlogCapabilities(list_posts=True, create_draft=True, idempotency_key=True),
        transport=httpx.MockTransport(handler),
    )
    stored_payload = {"article": {"title": "Title", "body": "Body", "language": "en", "state": "drafting"}}
    try:
        result = await adapter.send_payload(stored_payload, intent_key="stable-key", publish=False)
    finally:
        await adapter.close()
    assert result["http_status"] == 201
    assert seen == {"payload": stored_payload, "idempotency_key": "stable-key"}
