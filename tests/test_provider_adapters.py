from __future__ import annotations

from types import SimpleNamespace

import pytest

from takumi_research_agent.providers.anthropic_native import AnthropicProvider
from takumi_research_agent.providers.base import ModelMessage, ToolCall


@pytest.mark.asyncio
async def test_anthropic_tool_results_are_grouped_after_assistant_tool_use():
    captured = {}

    async def create(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text="done")],
            stop_reason="end_turn",
            usage=SimpleNamespace(input_tokens=10, output_tokens=2),
        )

    provider = AnthropicProvider.__new__(AnthropicProvider)
    provider.config = SimpleNamespace(model="claude-test")
    provider.client = SimpleNamespace(messages=SimpleNamespace(create=create))
    reply = await provider.complete(
        [
            ModelMessage("user", "Find evidence"),
            ModelMessage("assistant", tool_calls=(ToolCall("call-a", "search", {}), ToolCall("call-b", "read", {}))),
            ModelMessage("tool", '{"items":[]}', tool_call_id="call-a", tool_name="search"),
            ModelMessage("tool", '{"text":"source"}', tool_call_id="call-b", tool_name="read"),
        ],
        [],
        max_output_tokens=200,
    )
    wire = captured["messages"]
    assert [message["role"] for message in wire] == ["user", "assistant", "user"]
    assert [block["tool_use_id"] for block in wire[2]["content"]] == ["call-a", "call-b"]
    assert reply.content == "done"
    assert reply.stop_reason == "complete"
