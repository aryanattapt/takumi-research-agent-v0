from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urljoin, urlsplit

import httpx
import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator


class ContractError(ValueError):
    pass


class Operation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation_id: str
    method: Literal["get", "post", "put", "patch", "delete"]
    path: str
    summary: str = ""


class BlogMapping(BaseModel):
    """A small declarative mapping language; values are paths, never executable code."""
    model_config = ConfigDict(extra="forbid")
    list_path: str
    response_fields: dict[str, str] = Field(min_length=2, max_length=20)
    create_fields: dict[str, str] = Field(min_length=2, max_length=30)
    publish_status_values: dict[str, str] = Field(default_factory=lambda: {"draft": "draft", "publish": "published"})
    pagination_mode: Literal["none", "cursor", "offset", "page"] = "none"
    page_next_path: str | None = None
    page_request_parameter: str | None = None
    page_size_parameter: str | None = None
    page_size: int = Field(default=100, ge=1, le=1_000)
    page_limit: int = Field(default=1, ge=1, le=100)
    delta_since_parameter: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_.-]{1,120}$")
    delta_time_format: Literal["iso8601", "unix_seconds"] = "iso8601"
    delta_overlap_seconds: int = Field(default=300, ge=0, le=604_800)
    response_fields_required: set[str] = Field(default_factory=lambda: {"id", "title"})

    @field_validator("list_path", "page_next_path")
    @classmethod
    def validate_json_path(cls, value: str | None) -> str | None:
        if value is not None:
            parse_path(value)
        return value

    @field_validator("response_fields", "create_fields")
    @classmethod
    def validate_paths(cls, value: dict[str, str]) -> dict[str, str]:
        for path in value.values():
            parse_path(path)
        return value

    @model_validator(mode="after")
    def validate_pagination(self):
        if self.pagination_mode == "cursor" and (not self.page_next_path or not self.page_request_parameter):
            raise ValueError("Cursor pagination requires page_next_path and page_request_parameter")
        if self.pagination_mode in {"offset", "page"} and (not self.page_request_parameter or not self.page_size_parameter):
            raise ValueError("Page and offset pagination require page and page-size request parameters")
        if self.page_next_path and self.pagination_mode != "cursor":
            raise ValueError("page_next_path is only valid with cursor pagination")
        if self.delta_since_parameter and self.delta_since_parameter in {self.page_request_parameter, self.page_size_parameter}:
            raise ValueError("Delta-since parameter must be distinct from pagination parameters")
        return self


@dataclass(frozen=True, slots=True)
class LoadedContract:
    title: str
    base_url: str
    operations: tuple[Operation, ...]
    raw: dict[str, Any]


def parse_path(path: str) -> tuple[str | int, ...]:
    """Parse a restricted JSONPath subset: $.field, $[0], and $['field']."""
    if not isinstance(path, str) or not path.startswith("$"):
        raise ContractError("Mapping paths must start with '$'")
    tokens: list[str | int] = []
    cursor = 1
    while cursor < len(path):
        if path[cursor] == ".":
            match = re.match(r"\.([A-Za-z_][A-Za-z0-9_-]*)", path[cursor:])
            if not match:
                raise ContractError(f"Invalid mapping path near {path[cursor:]!r}")
            tokens.append(match.group(1))
            cursor += len(match.group(0))
        elif path[cursor] == "[":
            match = re.match(r"\[(?:(\d+)|'([^']+)'|\"([^\"]+)\")\]", path[cursor:])
            if not match:
                raise ContractError("Array indexes and quoted object keys are the only supported bracket selectors")
            tokens.append(int(match.group(1)) if match.group(1) is not None else (match.group(2) or match.group(3)))
            cursor += len(match.group(0))
        else:
            raise ContractError(f"Invalid mapping path near {path[cursor:]!r}")
    return tuple(tokens)


def get_path(value: Any, path: str, *, missing: Any = None) -> Any:
    current = value
    try:
        for token in parse_path(path):
            current = current[token]
        return current
    except (KeyError, IndexError, TypeError):
        return missing


def set_path(value: dict[str, Any], path: str, item: Any) -> None:
    tokens = parse_path(path)
    if not tokens or not all(isinstance(token, str) for token in tokens):
        raise ContractError("Request mapping targets must be non-empty object paths")
    current = value
    for token in tokens[:-1]:
        child = current.get(token)
        if child is None:
            child = {}
            current[token] = child
        if not isinstance(child, dict):
            raise ContractError(f"Conflicting target mapping at {token!r}")
        current = child
    current[tokens[-1]] = item


def infer_response_mapping(example: Any) -> dict[str, str]:
    """Suggest canonical field paths from a sample. Suggestions must be reviewed before use."""
    aliases = {
        "id": {"id", "postid", "articleid", "uuid", "slug"},
        "title": {"title", "name", "headline", "judul"},
        "content": {"content", "body", "bodyhtml", "text", "description", "isi"},
        "language": {"language", "locale", "lang", "bahasa"},
        "status": {"status", "state", "poststatus"},
        "published_at": {"publishedat", "published", "publicationdate", "date", "createdat"},
        "url": {"url", "link", "permalink"},
    }
    matches: dict[str, list[str]] = {field: [] for field in aliases}

    def visit(value: Any, path: str) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                normalized = re.sub(r"[^a-z0-9]", "", str(key).lower())
                child_path = f"{path}['{key}']" if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]*", str(key)) else f"{path}.{key}"
                for field, names in aliases.items():
                    if normalized in names and child is not None and not isinstance(child, (dict, list)):
                        matches[field].append(child_path)
                visit(child, child_path)
        elif isinstance(value, list):
            for index, child in enumerate(value[:50]):
                visit(child, f"{path}[{index}]")

    visit(example, "$")
    return {field: paths[0] for field, paths in matches.items() if len(paths) == 1}


def infer_collection_path(example: Any) -> str | None:
    candidates: list[tuple[int, str]] = []

    def visit(value: Any, path: str) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                child_path = f"{path}['{key}']" if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]*", str(key)) else f"{path}.{key}"
                if isinstance(child, list) and child and all(isinstance(item, dict) for item in child[:20]):
                    alias_fields = set(infer_response_mapping(child[0]))
                    score = len(alias_fields.intersection({"id", "title", "content"}))
                    if score >= 2:
                        candidates.append((score, child_path))
                visit(child, child_path)
        elif isinstance(value, list):
            for index, child in enumerate(value[:20]):
                visit(child, f"{path}[{index}]")

    visit(example, "$")
    if not candidates:
        return None
    best_score = max(item[0] for item in candidates)
    best = sorted(path for score, path in candidates if score == best_score)
    return best[0] if len(best) == 1 else None


def normalize_records(payload: Any, mapping: BlogMapping) -> list[dict[str, Any]]:
    records = get_path(payload, mapping.list_path)
    if not isinstance(records, list):
        raise ContractError("Configured list path did not resolve to an array")
    normalized = []
    for index, record in enumerate(records):
        if not isinstance(record, dict):
            raise ContractError(f"List item {index} is not a JSON object")
        item = {field: get_path(record, path) for field, path in mapping.response_fields.items()}
        if item.get("status") is not None and mapping.publish_status_values:
            reverse_status = {remote: canonical for canonical, remote in mapping.publish_status_values.items()}
            item["status"] = reverse_status.get(item["status"], item["status"])
        missing = mapping.response_fields_required.intersection(key for key, value in item.items() if value in (None, ""))
        if missing:
            raise ContractError(f"List item {index} lacks mapped required fields: {', '.join(sorted(missing))}")
        normalized.append(item)
    return normalized


def has_unmapped_pagination_hint(payload: Any, list_path: str) -> bool:
    hints = {"next", "nextcursor", "nextpage", "next_page", "nexttoken", "next_token", "hasmore", "has_more", "continuationtoken"}
    current = payload
    for token in parse_path(list_path):
        if isinstance(current, dict):
            for key, value in current.items():
                normalized = re.sub(r"[^a-z0-9_]", "", str(key).casefold())
                if normalized in hints and value not in (None, "", False, [], {}):
                    return True
        try:
            current = current[token]
        except (KeyError, IndexError, TypeError):
            return False
    return False


def build_create_payload(canonical: dict[str, Any], mapping: BlogMapping) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for canonical_field, target_path in mapping.create_fields.items():
        if canonical_field not in canonical:
            raise ContractError(f"Canonical input lacks required mapped value {canonical_field!r}")
        value = canonical[canonical_field]
        if canonical_field == "status":
            if value not in mapping.publish_status_values:
                raise ContractError(f"No validated remote status value is configured for {value!r}")
            value = mapping.publish_status_values[value]
        set_path(payload, target_path, value)
    return payload


class OpenApiLoader:
    def __init__(self, *, allowed_hosts: set[str] | None = None, transport: httpx.AsyncBaseTransport | None = None):
        self.allowed_hosts = {host.lower() for host in (allowed_hosts or set())}
        self.client = httpx.AsyncClient(timeout=httpx.Timeout(15, connect=5), follow_redirects=False, transport=transport)

    async def close(self) -> None:
        await self.client.aclose()

    async def load(self, source: str, *, base_url_override: str | None = None) -> LoadedContract:
        if source.startswith(("https://", "http://")):
            parsed = urlsplit(source)
            if (
                parsed.scheme != "https"
                or not parsed.hostname
                or parsed.username
                or parsed.password
                or parsed.query
                or parsed.fragment
                or parsed.hostname.lower() not in self.allowed_hosts
            ):
                raise ContractError("Remote OpenAPI URL must be HTTPS and on the administrator allowlist")
            async with self.client.stream("GET", source) as response:
                response.raise_for_status()
                content_length = response.headers.get("content-length")
                if content_length:
                    try:
                        declared_bytes = int(content_length)
                    except ValueError as exc:
                        raise ContractError("OpenAPI contract has an invalid Content-Length") from exc
                    if declared_bytes > 2_000_000:
                        raise ContractError("OpenAPI contract exceeds 2 MB")
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(body) + len(chunk) > 2_000_000:
                        raise ContractError("OpenAPI contract exceeds 2 MB")
                    body.extend(chunk)
                try:
                    text = bytes(body).decode(response.encoding or "utf-8")
                except UnicodeDecodeError as exc:
                    raise ContractError("OpenAPI contract is not valid UTF-8") from exc
        else:
            path = Path(source).expanduser().resolve()
            if not path.is_file() or path.stat().st_size > 2_000_000:
                raise ContractError("OpenAPI contract file is missing or exceeds 2 MB")
            text = path.read_text(encoding="utf-8")
        try:
            raw = json.loads(text) if text.lstrip().startswith(("{", "[")) else yaml.safe_load(text)
        except (json.JSONDecodeError, yaml.YAMLError) as exc:
            raise ContractError("OpenAPI contract is not valid JSON or YAML") from exc
        if not isinstance(raw, dict) or not isinstance(raw.get("paths"), dict):
            raise ContractError("OpenAPI document must contain a paths object")
        operations: list[Operation] = []
        for path, methods in raw["paths"].items():
            if not isinstance(methods, dict):
                continue
            for method, spec in methods.items():
                if method.lower() not in {"get", "post", "put", "patch", "delete"} or not isinstance(spec, dict):
                    continue
                operation_id = spec.get("operationId") or f"{method.lower()}_{re.sub(r'[^a-zA-Z0-9]+', '_', path).strip('_')}"
                operations.append(Operation(operation_id=operation_id, method=method.lower(), path=path, summary=spec.get("summary", "")))
        servers = raw.get("servers") or []
        contract_base = servers[0].get("url", "") if servers and isinstance(servers[0], dict) else ""
        return LoadedContract(
            title=str(raw.get("info", {}).get("title", "Blog API"))[:200],
            base_url=base_url_override or contract_base,
            operations=tuple(operations),
            raw=raw,
        )
