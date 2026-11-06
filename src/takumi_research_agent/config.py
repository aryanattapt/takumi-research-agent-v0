from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


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
