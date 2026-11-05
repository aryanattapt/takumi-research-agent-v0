from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


@dataclass(frozen=True, slots=True)
class Settings:
    model_base_url: str
    model_name: str
    model_api_key: str
    blog_base_url: str
    blog_api_token: str
    blog_api_spec: str
    blog_api_auth_header: str
    blog_api_auth_scheme: str
    data_dir: Path
    headless: bool
    timezone: str
    allowed_domains: tuple[str, ...]
    max_agent_steps: int
    max_research_retries: int

    @classmethod
    def from_env(cls, env_file: str | Path | None = ".env") -> "Settings":
        if env_file:
            load_dotenv(Path(env_file), override=False)
        domains = tuple(
            item.strip().lower()
            for item in os.getenv(
                "RESEARCH_ALLOWED_DOMAINS",
                "crossref.org,api.crossref.org,europepmc.org,ebi.ac.uk,doaj.org,doi.org,pmc.ncbi.nlm.nih.gov,joss.theoj.org",
            ).split(",")
            if item.strip()
        )
        return cls(
            model_base_url=os.getenv("MODEL_BASE_URL", "http://localhost:11434/v1").rstrip("/"),
            model_name=os.getenv("MODEL_NAME", "qwen3"),
            model_api_key=os.getenv("MODEL_API_KEY", "ollama"),
            blog_base_url=os.getenv("BLOG_BASE_URL", "").rstrip("/"),
            blog_api_token=os.getenv("BLOG_API_TOKEN", ""),
            blog_api_spec=os.getenv("BLOG_API_SPEC", ""),
            blog_api_auth_header=os.getenv("BLOG_API_AUTH_HEADER", ""),
            blog_api_auth_scheme=os.getenv("BLOG_API_AUTH_SCHEME", "Bearer"),
            data_dir=Path(os.getenv("DATA_DIR", "./data")).expanduser(),
            headless=os.getenv("AGENT_HEADLESS", "true").strip().lower() not in {"0", "false", "no"},
            timezone=os.getenv("TZ", "Asia/Jakarta"),
            allowed_domains=domains,
            max_agent_steps=max(5, int(os.getenv("MAX_AGENT_STEPS", "40"))),
            max_research_retries=max(0, int(os.getenv("MAX_RESEARCH_RETRIES", "2"))),
        )

    def validate_runtime(self) -> list[str]:
        problems = []
        if not self.model_base_url:
            problems.append("MODEL_BASE_URL is required")
        if not self.model_name:
            problems.append("MODEL_NAME is required")
        if not self.blog_base_url:
            problems.append("BLOG_BASE_URL is required")
        if not self.blog_api_spec:
            problems.append("BLOG_API_SPEC is required")
        if not self.allowed_domains:
            problems.append("RESEARCH_ALLOWED_DOMAINS must contain at least one domain")
        return problems
