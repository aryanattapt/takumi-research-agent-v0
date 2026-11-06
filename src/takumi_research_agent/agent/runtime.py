from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Literal

from pydantic import BaseModel, ValidationError

from takumi_research_agent.providers.base import ModelMessage, ModelProvider, ModelReply, ToolCall


Stage = Literal["research", "write", "review", "recovery"]


class BudgetExhausted(RuntimeError):
    pass


class AgentStopped(RuntimeError):
    def __init__(self, reason: str, detail: str):
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


@dataclass(slots=True)
class RequestBudget:
    max_requests: int = 20
    max_tool_actions: int = 40
    stage_limits: dict[Stage, int] | None = None
    physical_requests: int = 0
    tool_actions: int = 0
    stage_usage: dict[Stage, int] | None = None

    def __post_init__(self) -> None:
        self.stage_limits = self.stage_limits or {"research": 12, "write": 2, "review": 2, "recovery": 4}
        self.stage_usage = self.stage_usage or {key: 0 for key in self.stage_limits}

    def reserve_request(self, stage: Stage) -> None:
        assert self.stage_usage is not None and self.stage_limits is not None
        if self.physical_requests >= self.max_requests:
            raise BudgetExhausted("Global physical model request budget exhausted")
        if self.stage_usage[stage] >= self.stage_limits[stage]:
            raise BudgetExhausted(f"{stage} model request reserve exhausted")
        self.physical_requests += 1
        self.stage_usage[stage] += 1

    def reserve_tool_action(self) -> None:
        if self.tool_actions >= self.max_tool_actions:
            raise BudgetExhausted("Tool action budget exhausted")
        self.tool_actions += 1


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    name: str
    description: str
    arguments_model: type[BaseModel]
    handler: Callable[[BaseModel], Awaitable[Any]]
    requires_blog_scope: bool = False
    read_only: bool = True

    def provider_schema(self) -> dict[str, Any]:
        return {"name": self.name, "description": self.description, "parameters": self.arguments_model.model_json_schema()}


class ToolRegistry:
    def __init__(self, *, blog_id: str | None = None, allowed_names: set[str] | None = None):
        self.blog_id = blog_id
        self.allowed_names = allowed_names
        self._tools: dict[str, ToolDefinition] = {}

    def register(self, tool: ToolDefinition) -> None:
        if tool.name in self._tools:
            raise ValueError(f"Duplicate tool name: {tool.name}")
        self._tools[tool.name] = tool

    def available(self) -> list[ToolDefinition]:
        return [tool for name, tool in self._tools.items() if self.allowed_names is None or name in self.allowed_names]

    async def execute(self, call: ToolCall) -> Any:
        tool = self._tools.get(call.name)
        if tool is None or (self.allowed_names is not None and call.name not in self.allowed_names):
            raise PermissionError(f"Tool is not authorized for this run: {call.name}")
        if tool.requires_blog_scope and not self.blog_id:
            raise PermissionError("This tool requires an explicit blog scope")
        try:
            arguments = tool.arguments_model.model_validate(call.arguments)
        except ValidationError as exc:
            raise ValueError(f"Invalid arguments for {call.name}: {exc.errors(include_input=False)}") from exc
        return await tool.handler(arguments)


@dataclass(frozen=True, slots=True)
class AgentResult:
    status: Literal["completed", "blocked", "budget_exhausted", "no_progress", "incomplete", "refused"]
    content: str
    reason: str
    physical_requests: int
    tool_actions: int
    trace: tuple[dict[str, Any], ...]


def _fingerprint(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode()
    return hashlib.sha256(encoded).hexdigest()


class AgentRuntime:
    """Explicit observe/action loop with bounded requests and application-owned authorization."""

    def __init__(
        self,
        provider: ModelProvider,
        tools: ToolRegistry,
        *,
        budget: RequestBudget | None = None,
        max_steps: int = 40,
        max_repeated_observation: int = 2,
    ):
        self.provider = provider
        self.tools = tools
        self.budget = budget or RequestBudget()
        self.max_steps = min(max_steps, self.budget.max_tool_actions)
        self.max_repeated_observation = max_repeated_observation

    async def run(self, prompt: str, *, system: str, stage: Stage = "research") -> AgentResult:
        messages = [ModelMessage("system", system), ModelMessage("user", prompt)]
        trace: list[dict[str, Any]] = []
        observations: dict[str, int] = {}
        try:
            for step in range(self.max_steps + 1):
                self.budget.reserve_request(stage)
                reply = await self.provider.complete(
                    messages,
                    [item.provider_schema() for item in self.tools.available()],
                    max_output_tokens=4_000,
                    temperature=0,
                )
                trace.append({"type": "model_response", "step": step, "stop_reason": reply.stop_reason, "tokens_in": reply.usage_input_tokens, "tokens_out": reply.usage_output_tokens})
                if reply.stop_reason == "refused":
                    return self._result("refused", "", "provider_refused", trace)
                if reply.stop_reason == "incomplete":
                    return self._result("incomplete", reply.content, "provider_incomplete", trace)
                if reply.stop_reason == "schema_invalid":
                    return self._result("blocked", "", "provider_schema_invalid", trace)
                if not reply.tool_calls:
                    if not reply.content.strip():
                        return self._result("incomplete", "", "empty_final_response", trace)
                    return self._result("completed", reply.content, "agent_finished", trace)

                messages.append(ModelMessage("assistant", reply.content, reply.tool_calls))
                for call in reply.tool_calls:
                    self.budget.reserve_tool_action()
                    call_hash = _fingerprint({"name": call.name, "arguments": call.arguments})
                    try:
                        observation = await self.tools.execute(call)
                        observation_hash = _fingerprint(observation)
                        repeat_key = f"{call_hash}:{observation_hash}"
                        observations[repeat_key] = observations.get(repeat_key, 0) + 1
                        trace.append({"type": "tool", "name": call.name, "call_id": call.call_id, "arguments_hash": call_hash, "observation_hash": observation_hash, "status": "ok"})
                        if observations[repeat_key] >= self.max_repeated_observation:
                            return self._result("no_progress", "", "repeated_identical_observation", trace)
                        result_text = json.dumps(observation, ensure_ascii=False, default=str)
                    except PermissionError as exc:
                        trace.append({"type": "tool", "name": call.name, "call_id": call.call_id, "status": "denied", "reason": str(exc)})
                        result_text = json.dumps({"error": "permission_denied", "detail": str(exc)}, ensure_ascii=False)
                    except (ValueError, TypeError) as exc:
                        trace.append({"type": "tool", "name": call.name, "call_id": call.call_id, "status": "invalid", "reason": str(exc)[:300]})
                        result_text = json.dumps({"error": "invalid_tool_request", "detail": str(exc)[:300]}, ensure_ascii=False)
                    messages.append(ModelMessage("tool", result_text, tool_call_id=call.call_id, tool_name=call.name))
            return self._result("no_progress", "", "step_limit_reached", trace)
        except BudgetExhausted as exc:
            return self._result("budget_exhausted", "", str(exc), trace)

    def _result(self, status: AgentResult.__annotations__["status"], content: str, reason: str, trace: list[dict[str, Any]]) -> AgentResult:
        return AgentResult(status, content, reason, self.budget.physical_requests, self.budget.tool_actions, tuple(trace))
