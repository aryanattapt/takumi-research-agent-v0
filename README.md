# Takumi Research Agent

Creator: Codex

Autonomous agent for finding recent peer-reviewed research, explaining it in Indonesian and English, and publishing it through a blog API.

## Current snapshot

This first push contains project setup, validated research data models, SQLite storage, free literature discovery through Crossref and Europe PMC, open-access full-text retrieval, and a DOAJ-based peer-review evidence check.

It is an implementation foundation, not an operating agent yet. The Browser Use research loop, dynamic blog API adapter, bilingual drafting and review, CLI, publisher, scheduler, and automated tests remain to be built. Do not schedule it or use it to publish yet.

## Requirements

- Python 3.11 or newer.
- Chromium installed for the planned local headless browser.
- A model endpoint that supports tool calling. Ollama is the default target; model inference can run locally.
- A blog API with a readable OpenAPI contract before live history sync or publishing can be configured.

Research discovery uses public APIs without a paid search subscription. Local inference avoids a hosted-model bill, but still uses the machine, power, and internet.

## Install dependencies

    python -m venv .venv
    .\.venv\Scripts\Activate.ps1
    python -m pip install -r requirements.txt
    Copy-Item .env.example .env

requirements.txt installs the package plus Browser Use and PDF support. requirements-dev.txt also adds pytest.

## Configuration

Edit .env after copying the example. Keep real tokens out of Git.

- MODEL_BASE_URL, MODEL_NAME, MODEL_API_KEY: model endpoint and credentials.
- BLOG_BASE_URL, BLOG_API_SPEC, BLOG_API_TOKEN: blog connection settings.
- DATA_DIR: persistent SQLite and extracted research data.
- AGENT_HEADLESS, RESEARCH_ALLOWED_DOMAINS: local browser controls.
- TZ: daily slot timezone; defaults to Asia/Jakarta.

The blog endpoint shape will be loaded from OpenAPI at runtime. Unsupported or ambiguous contracts must fail closed rather than guess a publish payload.

## Development state

The implementation follows the staged plan: prove source/model/browser fit, build a fixture-driven agent loop, add real retrieval and topic history, then enable guarded publishing and scheduling. Missing blog API details do not block source and agent development, but live publishing cannot be completed until that contract is available.
