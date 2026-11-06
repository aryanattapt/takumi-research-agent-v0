from __future__ import annotations

from typing import Any

from anthropic import AsyncAnthropic

from .base import ModelMessage, ModelReply, ProviderConfig, ToolCall


class AnthropicProvider:
    """Native Messages API adapter; it does not emulate the OpenAI wire format."""

    def __init__(self, config: ProviderConfig):
        self.config = config
        self.client = AsyncAnthropic(
            api_key=config.api_key,
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
        system = "\n\n".join(item.content for item in messages if item.role == "system")
        wire_messages: list[dict[str, Any]] = []
        index = 0
        while index < len(messages):
            message = messages[index]
            if message.role in {"system"}:
                index += 1
                continue
            if message.role == "tool":
                results = []
                while index < len(messages) and messages[index].role == "tool":
                    tool_message = messages[index]
                    results.append({
                        "type": "tool_result",
                        "tool_use_id": tool_message.tool_call_id,
                        "content": tool_message.content,
                    })
                    index += 1
                wire_messages.append({
                    "role": "user",
                    "content": results,
                })
                continue
            index += 1
            if message.role == "assistant" and message.tool_calls:
                blocks: list[dict[str, Any]] = []
                if message.content:
                    blocks.append({"type": "text", "text": message.content})
                blocks.extend({"type": "tool_use", "id": call.call_id, "name": call.name, "input": call.arguments} for call in message.tool_calls)
                wire_messages.append({"role": "assistant", "content": blocks})
            else:
                wire_messages.append({"role": message.role, "content": message.content})

        response = await self.client.messages.create(
            model=self.config.model,
            system=system or "You are a research assistant. Follow tool and evidence boundaries.",
            messages=wire_messages,
            tools=[{"name": tool["name"], "description": tool["description"], "input_schema": tool["parameters"]} for tool in tools] or None,
            max_tokens=max_output_tokens,
            temperature=temperature,
        )
        text_parts, calls = [], []
        for block in response.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                if not isinstance(block.input, dict):
                    return ModelReply("", stop_reason="schema_invalid", raw_finish_reason=response.stop_reason)
                calls.append(ToolCall(block.id, block.name, block.input))
        if response.stop_reason == "refusal":
            stop = "refused"
        elif response.stop_reason == "max_tokens":
            stop = "incomplete"
        elif calls:
            stop = "tool_call"
        else:
            stop = "complete"
        usage = response.usage
        return ModelReply(
            content="\n".join(text_parts),
            tool_calls=tuple(calls),
            stop_reason=stop,
            usage_input_tokens=usage.input_tokens,
            usage_output_tokens=usage.output_tokens,
            raw_finish_reason=response.stop_reason,
        )
