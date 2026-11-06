from __future__ import annotations

import json
from typing import Any

from openai import AsyncOpenAI

from .base import ModelMessage, ModelReply, ProviderConfig, ToolCall


def _tool_schema(tool: dict[str, Any]) -> dict[str, Any]:
    return {"type": "function", "function": tool}


class OpenAICompatibleProvider:
    """Chat Completions adapter, including local OpenAI-compatible servers."""

    def __init__(self, config: ProviderConfig):
        self.config = config
        self.client = AsyncOpenAI(
            api_key=config.api_key or "not-required",
            base_url=config.base_url,
            timeout=config.timeout_seconds,
            max_retries=0,
            default_headers=config.extra_headers,
        )

    async def close(self) -> None:
        await self.client.close()

    async def complete(
        self,
        messages: list[ModelMessage],
        tools: list[dict[str, Any]],
        *,
        max_output_tokens: int,
        temperature: float = 0,
    ) -> ModelReply:
        wire_messages: list[dict[str, Any]] = []
        for message in messages:
            if message.role == "tool":
                wire_messages.append({"role": "tool", "tool_call_id": message.tool_call_id, "content": message.content})
                continue
            item: dict[str, Any] = {"role": message.role, "content": message.content or None}
            if message.role == "assistant" and message.tool_calls:
                item["tool_calls"] = [
                    {
                        "id": call.call_id,
                        "type": "function",
                        "function": {"name": call.name, "arguments": json.dumps(call.arguments, ensure_ascii=False)},
                    }
                    for call in message.tool_calls
                ]
            wire_messages.append(item)

        response = await self.client.chat.completions.create(
            model=self.config.model,
            messages=wire_messages,
            tools=[_tool_schema(tool) for tool in tools] or None,
            tool_choice="auto" if tools else None,
            max_tokens=max_output_tokens,
            temperature=temperature,
        )
        choice = response.choices[0]
        message = choice.message
        calls = []
        for item in message.tool_calls or []:
            try:
                args = json.loads(item.function.arguments or "{}")
            except json.JSONDecodeError:
                return ModelReply("", stop_reason="schema_invalid", raw_finish_reason=choice.finish_reason)
            if not isinstance(args, dict):
                return ModelReply("", stop_reason="schema_invalid", raw_finish_reason=choice.finish_reason)
            calls.append(ToolCall(item.id, item.function.name, args))
        if getattr(message, "refusal", None) or choice.finish_reason == "content_filter":
            stop = "refused"
        elif choice.finish_reason == "length":
            stop = "incomplete"
        elif calls:
            stop = "tool_call"
        else:
            stop = "complete"
        usage = response.usage
        return ModelReply(
            content=message.content or "",
            tool_calls=tuple(calls),
            stop_reason=stop,
            usage_input_tokens=usage.prompt_tokens if usage else None,
            usage_output_tokens=usage.completion_tokens if usage else None,
            raw_finish_reason=choice.finish_reason,
        )
