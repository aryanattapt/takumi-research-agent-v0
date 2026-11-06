from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol


@dataclass(frozen=True, slots=True)
class ToolCall:
    call_id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ModelMessage:
    role: Literal["system", "user", "assistant", "tool"]
    content: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    tool_call_id: str | None = None
    tool_name: str | None = None


@dataclass(frozen=True, slots=True)
class ModelReply:
    content: str
    tool_calls: tuple[ToolCall, ...] = ()
    stop_reason: Literal["complete", "tool_call", "refused", "incomplete", "schema_invalid"] = "complete"
    usage_input_tokens: int | None = None
    usage_output_tokens: int | None = None
    raw_finish_reason: str | None = None


class ModelProvider(Protocol):
    async def complete(
        self,
        messages: list[ModelMessage],
        tools: list[dict[str, Any]],
        *,
        max_output_tokens: int,
        temperature: float = 0,
    ) -> ModelReply: ...


@dataclass(slots=True)
class ProviderConfig:
    model: str
    api_key: str
    base_url: str | None = None
    timeout_seconds: float = 90
    extra_headers: dict[str, str] = field(default_factory=dict)
