from __future__ import annotations

import json
from collections import deque
from pathlib import Path

import pytest
from pydantic import BaseModel

from takumi_research_agent.agent.runtime import AgentRuntime, RequestBudget, ToolDefinition, ToolRegistry
from takumi_research_agent.providers.base import ModelReply, ToolCall


GOLD_DATASET = json.loads((Path(__file__).parent / "fixtures" / "agent-runtime-gold-v1.json").read_text(encoding="utf-8"))


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
@pytest.mark.parametrize("case", GOLD_DATASET["cases"], ids=lambda case: case["id"])
async def test_versioned_gold_cases_check_observation_driven_actions(case):
    actions = []

    class AdaptiveFixtureProvider:
        def __init__(self):
            self.requests = 0

        async def complete(self, messages, _tools, *, max_output_tokens, temperature=0):
            self.requests += 1
            tool_messages = [message for message in messages if message.role == "tool"]
            if not tool_messages:
                return ModelReply("", (ToolCall("search", "literature.search", {}),), "tool_call")

            last_observation = tool_messages[-1]
            observation = json.loads(last_observation.content)
            if last_observation.tool_name == "literature.search":
                papers = observation.get("papers", [])
                if not papers:
                    return ModelReply("No eligible source was found; research stops here.")
                source_id = papers[0]["source_id"]
                return ModelReply("", (ToolCall("read", "sources.read", {"source_id": source_id}),), "tool_call")

            return ModelReply(f"Evidence checked: {observation['title']}. {observation['finding']}")

    async def search(_):
        actions.append("literature.search")
        return case["search_observation"]

    async def read(arguments):
        actions.append("sources.read")
        source = case["readable_source"]
        assert arguments.source_id == source["source_id"]
        return source

    registry = ToolRegistry(blog_id="blog-1")
    registry.register(ToolDefinition("literature.search", "Search sources", EmptyArgs, search))

    class ReadArgs(BaseModel):
        source_id: str

    registry.register(ToolDefinition("sources.read", "Read a source", ReadArgs, read))
    provider = AdaptiveFixtureProvider()
    budget = RequestBudget(max_requests=20, max_tool_actions=40)
    result = await AgentRuntime(provider, registry, budget=budget).run("Research the fixture topic", system="Use observations to decide what to read.")

    assert result.status == case["expected_status"]
    assert result.reason == case["expected_reason"]
    assert actions == case["expected_actions"]
    assert result.physical_requests == case["expected_physical_requests"]
    assert result.tool_actions == len(case["expected_actions"])
    assert case["expected_final_contains"] in result.content
    assert provider.requests == result.physical_requests


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
