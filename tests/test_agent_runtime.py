from __future__ import annotations

from collections import deque

import pytest
from pydantic import BaseModel

from takumi_research_agent.agent.runtime import AgentRuntime, RequestBudget, ToolDefinition, ToolRegistry
from takumi_research_agent.providers.base import ModelReply, ToolCall


class EmptyArgs(BaseModel):
    pass


class FakeProvider:
    def __init__(self, replies):
        self.replies = deque(replies)
        self.requests = 0

    async def complete(self, messages, tools, *, max_output_tokens, temperature=0):
        self.requests += 1
        return self.replies.popleft()


@pytest.mark.asyncio
async def test_tool_observation_changes_next_action_and_budget_is_counted():
    async def observe(_):
        return {"papers": 2}

    registry = ToolRegistry(blog_id="blog-1")
    registry.register(ToolDefinition("literature.search", "Search papers", EmptyArgs, observe))
    provider = FakeProvider([
        ModelReply("", (ToolCall("c1", "literature.search", {}),), "tool_call"),
        ModelReply("Found two papers; continue with the selected result.", stop_reason="complete"),
    ])
    budget = RequestBudget(max_requests=20, max_tool_actions=40)
    result = await AgentRuntime(provider, registry, budget=budget).run("Research", system="Use evidence.")
    assert result.status == "completed"
    assert result.physical_requests == 2
    assert result.tool_actions == 1
    assert result.content.startswith("Found two")


@pytest.mark.asyncio
async def test_unknown_tool_is_denied_without_calling_handler():
    called = False

    async def side_effect(_):
        nonlocal called
        called = True
        return {"changed": True}

    registry = ToolRegistry(blog_id="blog-1", allowed_names={"history.find_related"})
    registry.register(ToolDefinition("blog.publish", "Never allowed", EmptyArgs, side_effect, requires_blog_scope=True, read_only=False))
    provider = FakeProvider([
        ModelReply("", (ToolCall("evil", "blog.publish", {}),), "tool_call"),
        ModelReply("No write action was performed.", stop_reason="complete"),
    ])
    result = await AgentRuntime(provider, registry).run("Research", system="Read only.")
    assert result.status == "completed"
    assert not called
    assert any(item.get("status") == "denied" for item in result.trace)


@pytest.mark.asyncio
async def test_repeated_identical_observation_stops_agent_loop():
    async def observe(_):
        return {"same": "result"}

    registry = ToolRegistry()
    registry.register(ToolDefinition("sources.search", "Search", EmptyArgs, observe))
    provider = FakeProvider([
        ModelReply("", (ToolCall("one", "sources.search", {}),), "tool_call"),
        ModelReply("", (ToolCall("two", "sources.search", {}),), "tool_call"),
    ])
    result = await AgentRuntime(provider, registry).run("Research", system="No repeated calls.")
    assert result.status == "no_progress"
    assert result.reason == "repeated_identical_observation"
    assert result.tool_actions == 2


@pytest.mark.asyncio
async def test_refusal_is_not_retried_as_invalid_json():
    provider = FakeProvider([ModelReply("", stop_reason="refused")])
    result = await AgentRuntime(provider, ToolRegistry()).run("Research", system="System")
    assert result.status == "refused"
    assert result.physical_requests == 1
    assert provider.requests == 1
