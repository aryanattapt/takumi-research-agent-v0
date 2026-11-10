from __future__ import annotations

from typing import Any, Literal
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator


class StrictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class BootstrapRequest(StrictRequest):
    username: str = Field(min_length=3, max_length=120)
    password: str = Field(min_length=12, max_length=200)


class LoginRequest(StrictRequest):
    username: str = Field(min_length=1, max_length=120)
    password: str = Field(min_length=1, max_length=200)


class ProviderRequest(StrictRequest):
    name: str = Field(min_length=2, max_length=120)
    provider_type: Literal["openai_compatible", "anthropic"]
    base_url: str = Field(min_length=8, max_length=1_000)
    model: str = Field(min_length=1, max_length=200)
    api_key: str | None = Field(default=None, max_length=4_000)
    timeout_seconds: int = Field(default=90, ge=5, le=600)
    max_output_tokens: int = Field(default=4_000, ge=128, le=64_000)
    expected_revision: int | None = Field(default=None, ge=1)


class RSSFeedRequest(StrictRequest):
    name: str = Field(min_length=1, max_length=80)
    url: str = Field(min_length=8, max_length=2_000)
    field: Literal["technology", "physics", "chemistry", "biology"]

    @field_validator("url")
    @classmethod
    def validate_feed_url(cls, value: str) -> str:
        parsed = urlsplit(value)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
            raise ValueError("RSS feed URL must be absolute HTTPS without credentials or fragment")
        return value


class BlogRequest(StrictRequest):
    name: str = Field(min_length=2, max_length=160)
    base_url: str = Field(min_length=8, max_length=1_000)
    api_spec_source: str = Field(default="", max_length=2_000)
    auth_header: str = Field(default="Authorization", min_length=1, max_length=100)
    auth_scheme: str = Field(default="Bearer", max_length=60)
    api_token: str | None = Field(default=None, max_length=4_000)
    provider_id: str | None = None
    adapter_mapping: dict[str, Any] = Field(default_factory=dict)
    capabilities: dict[str, Any] = Field(default_factory=dict)
    topic_settings: dict[str, Any] = Field(default_factory=lambda: {"technology": True, "physics": True, "chemistry": True, "biology": True})
    editorial_settings: dict[str, Any] = Field(default_factory=dict)
    rss_feeds: list[RSSFeedRequest] = Field(default_factory=list, max_length=50)
    schedule_enabled: bool = False
    schedule_timezone: str = Field(default="Asia/Jakarta", min_length=3, max_length=100)
    publish_time: str = Field(default="06:00", pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    lead_minutes: int = Field(default=120, ge=0, le=1_440)
    active: bool = False
    expected_revision: int | None = Field(default=None, ge=1)

    @field_validator("schedule_timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("schedule_timezone must be a valid IANA timezone") from exc
        return value

    @field_validator("topic_settings")
    @classmethod
    def validate_topic_settings(cls, value: dict[str, Any]) -> dict[str, bool]:
        allowed = {"technology", "physics", "chemistry", "biology"}
        if set(value) - allowed or any(not isinstance(enabled, bool) for enabled in value.values()):
            raise ValueError("topic_settings must contain boolean values for supported fields only")
        if not any(value.values()):
            raise ValueError("At least one topic field must be enabled")
        return value

    @field_validator("adapter_mapping")
    @classmethod
    def validate_mapping(cls, value: dict[str, Any]) -> dict[str, Any]:
        if value:
            from takumi_research_agent.blogs.contracts import BlogMapping

            BlogMapping.model_validate(value)
        return value

    @field_validator("rss_feeds")
    @classmethod
    def validate_rss_feeds(cls, value: list[RSSFeedRequest]) -> list[RSSFeedRequest]:
        names: set[str] = set()
        urls: set[str] = set()
        for feed in value:
            parsed = urlsplit(feed.url)
            normalized_url = f"{parsed.scheme}://{parsed.netloc.lower()}{parsed.path.rstrip('/')}?{parsed.query}"
            name = feed.name.strip().casefold()
            if name in names or normalized_url in urls:
                raise ValueError("RSS feed names and URLs must be unique per profile")
            names.add(name)
            urls.add(normalized_url)
        return value


class MappingPreviewRequest(StrictRequest):
    sample_response: Any
    canonical_post: dict[str, Any] = Field(default_factory=dict)


class GlobalControlRequest(StrictRequest):
    content_mutations_paused: bool
    expected_revision: int = Field(ge=1)


class AdapterReadTestRequest(StrictRequest):
    list_operation_id: str = Field(min_length=1, max_length=200)


class AdapterCreateOperationRequest(StrictRequest):
    create_operation_id: str = Field(min_length=1, max_length=200)


class AdapterCreateTestRequest(StrictRequest):
    acknowledge_remote_write: bool


class ManualRunRequest(StrictRequest):
    execution_mode: Literal["dryrun", "sandbox"] = "dryrun"


class DeliveryDispatchRequest(StrictRequest):
    execution_mode: Literal["sandbox", "live"] = "sandbox"


class DeliveryReconcileRequest(StrictRequest):
    outcome: Literal["succeeded", "failed"]
    remote_id: str | None = Field(default=None, max_length=300)
    note: str | None = Field(default=None, max_length=500)
