from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from dotenv import load_dotenv


@dataclass(frozen=True, slots=True)
class RSSFeedConfig:
    name: str
    url: str
    field: Literal["technology", "physics", "chemistry", "biology"]


def _parse_rss_feeds(raw: str) -> tuple[RSSFeedConfig, ...]:
    try:
        values = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("TREND_RSS_FEEDS must be a JSON array") from exc
    if not isinstance(values, list) or len(values) > 50:
        raise ValueError("TREND_RSS_FEEDS must be an array with at most 50 feeds")
    fields = {"technology", "physics", "chemistry", "biology"}
    feeds: list[RSSFeedConfig] = []
    seen_names: set[str] = set()
    seen_urls: set[str] = set()
    for index, value in enumerate(values):
        if not isinstance(value, dict):
            raise ValueError(f"TREND_RSS_FEEDS[{index}] must be an object")
        name, url, field = value.get("name"), value.get("url"), value.get("field")
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > 80:
            raise ValueError(f"TREND_RSS_FEEDS[{index}].name must contain 1 to 80 characters")
        if not isinstance(url, str) or len(url) > 2_000:
            raise ValueError(f"TREND_RSS_FEEDS[{index}].url must be an absolute HTTPS URL")
        parsed = urlsplit(url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
            raise ValueError(f"TREND_RSS_FEEDS[{index}].url must be an absolute HTTPS URL without credentials or fragment")
        if not isinstance(field, str) or field not in fields:
            raise ValueError(f"TREND_RSS_FEEDS[{index}].field must be one of {', '.join(sorted(fields))}")
        normalized_name = name.strip().casefold()
        normalized_url = f"{parsed.scheme}://{parsed.netloc.lower()}{parsed.path.rstrip('/')}?{parsed.query}"
        if normalized_name in seen_names or normalized_url in seen_urls:
            raise ValueError("TREND_RSS_FEEDS names and URLs must be unique")
        seen_names.add(normalized_name)
        seen_urls.add(normalized_url)
        feeds.append(RSSFeedConfig(name.strip(), url, field))
    return tuple(feeds)


@dataclass(frozen=True, slots=True)
class Settings:
    database_url: str
    master_key: str
    data_dir: Path
    headless: bool
    timezone: str
    allowed_domains: tuple[str, ...]
    session_cookie_secure: bool
    bootstrap_username: str
    bootstrap_password: str
    allowed_private_hosts: tuple[str, ...]
    trend_rss_feeds: tuple[RSSFeedConfig, ...] = ()
    provider_requests_per_minute: int = 0
    history_embedding_model: str = ""
    history_embedding_api_key: str = ""
    history_embedding_base_url: str | None = None
    history_embedding_min_similarity: float = 0.75

    @classmethod
    def from_env(cls, env_file: str | Path | None = ".env") -> "Settings":
        if env_file:
            load_dotenv(Path(env_file), override=False)
        domains = tuple(
            item.strip().lower()
            for item in os.getenv(
                "RESEARCH_ALLOWED_DOMAINS",
                "api.crossref.org,europepmc.org,ebi.ac.uk,doaj.org,doi.org,pmc.ncbi.nlm.nih.gov,arxiv.org,export.arxiv.org,hn.algolia.com,joss.theoj.org",
            ).split(",")
            if item.strip()
        )
        raw_provider_rpm = os.getenv("PROVIDER_REQUESTS_PER_MINUTE", "0").strip()
        try:
            provider_requests_per_minute = int(raw_provider_rpm)
        except ValueError as exc:
            raise ValueError("PROVIDER_REQUESTS_PER_MINUTE must be an integer between 0 and 100000") from exc
        if not 0 <= provider_requests_per_minute <= 100_000:
            raise ValueError("PROVIDER_REQUESTS_PER_MINUTE must be an integer between 0 and 100000")

        history_embedding_model = os.getenv("HISTORY_EMBEDDING_MODEL", "").strip()
        history_embedding_api_key = os.getenv("HISTORY_EMBEDDING_API_KEY", "")
        history_embedding_base_url = os.getenv("HISTORY_EMBEDDING_BASE_URL", "").strip() or None
        if bool(history_embedding_model) != bool(history_embedding_api_key):
            raise ValueError("HISTORY_EMBEDDING_MODEL and HISTORY_EMBEDDING_API_KEY must be configured together")
        try:
            history_embedding_min_similarity = float(os.getenv("HISTORY_EMBEDDING_MIN_SIMILARITY", "0.75"))
        except ValueError as exc:
            raise ValueError("HISTORY_EMBEDDING_MIN_SIMILARITY must be between 0 and 1") from exc
        if not math.isfinite(history_embedding_min_similarity) or not 0 <= history_embedding_min_similarity <= 1:
            raise ValueError("HISTORY_EMBEDDING_MIN_SIMILARITY must be between 0 and 1")
        if history_embedding_base_url:
            parsed_base_url = urlsplit(history_embedding_base_url)
            if parsed_base_url.scheme not in {"http", "https"} or not parsed_base_url.hostname or parsed_base_url.username or parsed_base_url.password or parsed_base_url.fragment:
                raise ValueError("HISTORY_EMBEDDING_BASE_URL must be an absolute HTTP(S) URL without credentials or fragment")

        return cls(
            database_url=os.getenv("DATABASE_URL", "postgresql+psycopg://takumi:takumi@localhost:5432/takumi"),
            master_key=os.getenv("TAKUMI_MASTER_KEY", ""),
            data_dir=Path(os.getenv("DATA_DIR", "./data")).expanduser(),
            headless=os.getenv("AGENT_HEADLESS", "true").strip().lower() not in {"0", "false", "no"},
            timezone=os.getenv("TZ", "Asia/Jakarta"),
            allowed_domains=domains,
            session_cookie_secure=os.getenv("SESSION_COOKIE_SECURE", "true").strip().lower() not in {"0", "false", "no"},
            bootstrap_username=os.getenv("BOOTSTRAP_ADMIN_USERNAME", ""),
            bootstrap_password=os.getenv("BOOTSTRAP_ADMIN_PASSWORD", ""),
            allowed_private_hosts=tuple(
                value.strip().lower()
                for value in os.getenv("ADMIN_ENDPOINT_ALLOWLIST", "").split(",")
                if value.strip()
            ),
            trend_rss_feeds=_parse_rss_feeds(os.getenv("TREND_RSS_FEEDS", "[]")),
            provider_requests_per_minute=provider_requests_per_minute,
            history_embedding_model=history_embedding_model,
            history_embedding_api_key=history_embedding_api_key,
            history_embedding_base_url=history_embedding_base_url,
            history_embedding_min_similarity=history_embedding_min_similarity,
        )

    def validate_runtime(self) -> list[str]:
        problems: list[str] = []
        if not self.database_url:
            problems.append("DATABASE_URL is required")
        if not self.master_key:
            problems.append("TAKUMI_MASTER_KEY is required for credential encryption")
        if not self.allowed_domains:
            problems.append("RESEARCH_ALLOWED_DOMAINS must contain at least one domain")
        return problems
