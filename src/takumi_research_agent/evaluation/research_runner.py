from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import subprocess
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from takumi_research_agent.agent.runtime import RequestBudget
from takumi_research_agent.config import Settings
from takumi_research_agent.providers.base import ProviderConfig
from takumi_research_agent.research.agent import ResearchToolbox
from takumi_research_agent.research.artifacts import ArtifactStore
from takumi_research_agent.research.browser import SourceRetriever
from takumi_research_agent.research.url_policy import PublicSourceUrlPolicy
from takumi_research_agent.sources import LiteratureSources

from .qualification import FIELDS, TRIAL_METADATA, TRIAL_METRICS

RUN_SCHEMA_VERSION = 1
REVIEW_SCHEMA_VERSION = 1
DEFAULT_TOOL_SCHEMA_VERSION = "research-tools-v1"
DEFAULT_POLICY_VERSION = "research-policy-v1"
PROVIDER_CHOICES = {"openai-compatible", "anthropic"}


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _document_fingerprint(value: dict[str, Any]) -> str:
    payload = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=".takumi-eval-",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def current_code_revision() -> str:
    override = os.getenv("TAKUMI_CODE_REVISION", "").strip()
    if override:
        return override
    try:
        revision = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout.strip()
        dirty = subprocess.run(
            ["git", "status", "--porcelain"],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout.strip() != ""
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError("Set --code-revision or TAKUMI_CODE_REVISION outside a git checkout") from exc
    return f"{revision}-dirty" if dirty else revision


def validate_research_run_dataset(dataset: dict[str, Any], *, split: str) -> list[dict[str, Any]]:
    if dataset.get("schema_version") != 1:
        raise ValueError("dataset schema_version must be 1")
    version = dataset.get("dataset_version")
    if not isinstance(version, str) or not version.strip():
        raise ValueError("dataset_version is required")
    cases = dataset.get("cases")
    if not isinstance(cases, list):
        raise TypeError("dataset cases must be an array")

    selected: list[dict[str, Any]] = []
    seen: set[str] = set()
    errors: list[str] = []
    for index, case in enumerate(cases):
        if not isinstance(case, dict):
            errors.append(f"cases[{index}] must be an object")
            continue
        case_id = case.get("case_id")
        if not isinstance(case_id, str) or not case_id.strip():
            errors.append(f"cases[{index}].case_id is required")
        elif case_id in seen:
            errors.append(f"duplicate case_id: {case_id}")
        else:
            seen.add(case_id)
        if case.get("split") not in {"development", "holdout"}:
            errors.append(f"cases[{index}].split must be development or holdout")
        if case.get("field") not in FIELDS:
            errors.append(f"cases[{index}].field must be one of the four supported fields")
        topic = case.get("topic")
        if not isinstance(topic, str) or not 12 <= len(topic.strip()) <= 240:
            errors.append(f"cases[{index}].topic must contain 12 to 240 characters")
        if not isinstance(case.get("history_complete"), bool):
            errors.append(f"cases[{index}].history_complete must be an explicit boolean")
        if not isinstance(case.get("blog_history"), list):
            errors.append(f"cases[{index}].blog_history must be an array; use [] for a known-empty snapshot")
        if not isinstance(case.get("trend_metrics", {}), dict):
            errors.append(f"cases[{index}].trend_metrics must be an object")
        if split == "all" or case.get("split") == split:
            selected.append(case)

    if errors:
        raise ValueError("; ".join(errors))
    if not selected:
        raise ValueError(f"dataset has no cases for split {split}")
    return selected


def _safe_case_id(case_id: str) -> str:
    value = re.sub(r"[^A-Za-z0-9_.-]+", "_", case_id).strip("._")
    suffix = hashlib.sha256(case_id.encode("utf-8")).hexdigest()[:10]
    return f"{value[:80] or 'case'}-{suffix}"


def _make_provider(provider_name: str, model: str, api_key: str, base_url: str | None, timeout: float):
    config = ProviderConfig(model=model, api_key=api_key, base_url=base_url, timeout_seconds=timeout)
    if provider_name == "openai-compatible":
        from takumi_research_agent.providers.openai_compatible import (
            OpenAICompatibleProvider,
        )

        return OpenAICompatibleProvider(config)
    if provider_name == "anthropic":
        from takumi_research_agent.providers.anthropic_native import AnthropicProvider

        return AnthropicProvider(config)
    raise ValueError(f"unsupported provider: {provider_name}")


def _serialize_research(research) -> dict[str, Any] | None:
    if research is None:
        return None
    return {
        "bundle": research.output.model_dump(mode="json"),
        "sources": [item.model_dump(mode="json") for item in research.sources],
        "evidence": [item.model_dump(mode="json") for item in research.evidence],
    }


async def _run_one_case(
    case: dict[str, Any],
    *,
    provider,
    settings: Settings,
    artifact_root: Path,
    browser_enabled: bool,
    max_steps: int,
) -> dict[str, Any]:
    case_id = str(case["case_id"])
    literature = LiteratureSources(timezone=settings.timezone)
    retriever = SourceRetriever(
        PublicSourceUrlPolicy(settings.allowed_domains),
        browser_enabled=browser_enabled,
    )
    budget = RequestBudget()
    toolbox = ResearchToolbox(
        provider=provider,
        budget=budget,
        literature=literature,
        retriever=retriever,
        artifacts=ArtifactStore(artifact_root),
        blog_id=f"evaluation-{_safe_case_id(case_id)}",
        blog_history=case["blog_history"],
        history_complete=case["history_complete"],
    )
    started = time.perf_counter()
    try:
        research, result = await toolbox.run(
            topic=case["topic"],
            field=case["field"],
            trend_metrics=case.get("trend_metrics", {}),
            max_steps=max_steps,
        )
        token_rows = [row for row in result.trace if row.get("type") == "model_response"]
        has_bundle = research is not None
        return {
            "status": result.status if has_bundle else "incomplete",
            "agent_status": result.status,
            "reason": result.reason if has_bundle else "missing_research_bundle",
            "duration_seconds": round(time.perf_counter() - started, 3),
            "physical_requests": result.physical_requests,
            "tool_actions": result.tool_actions,
            "tokens_input": sum(row.get("tokens_in") or 0 for row in token_rows),
            "tokens_output": sum(row.get("tokens_out") or 0 for row in token_rows),
            "research": _serialize_research(research),
            "trace": list(result.trace),
        }
    finally:
        await literature.close()
        await retriever.close()


def _empty_run_document(
    dataset_version: str,
    dataset_fingerprint: str,
    metadata: dict[str, str],
    split: str,
    trials_per_case: int,
) -> dict[str, Any]:
    return {
        "schema_version": RUN_SCHEMA_VERSION,
        "dataset_version": dataset_version,
        "dataset_fingerprint": dataset_fingerprint,
        "metadata": metadata,
        "split": split,
        "trials_per_case": trials_per_case,
        "created_at": _utc_now(),
        "runs": [],
    }


async def run_research_trials(
    dataset: dict[str, Any],
    *,
    output_path: Path,
    split: str,
    trials_per_case: int,
    provider_name: str,
    model: str,
    api_key: str,
    base_url: str | None,
    timeout_seconds: float,
    prompt_version: str,
    tool_schema_version: str,
    policy_version: str,
    adapter_version: str,
    code_revision: str,
    browser_enabled: bool = False,
    max_steps: int = 40,
    concurrency: int = 1,
    resume: bool = False,
    settings: Settings | None = None,
) -> dict[str, Any]:
    if provider_name not in PROVIDER_CHOICES:
        raise ValueError(f"provider must be one of {', '.join(sorted(PROVIDER_CHOICES))}")
    if not model.strip() or not api_key:
        raise ValueError("model and API key are required")
    if trials_per_case < 1:
        raise ValueError("trials_per_case must be positive")
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    if max_steps < 1:
        raise ValueError("max_steps must be positive")
    if not 1 <= concurrency <= 10:
        raise ValueError("concurrency must be between 1 and 10")
    selected = validate_research_run_dataset(dataset, split=split)
    dataset_version = str(dataset["dataset_version"])
    dataset_fingerprint = _document_fingerprint(dataset)
    metadata = {
        "provider": provider_name,
        "model": model,
        "prompt_version": prompt_version,
        "tool_schema_version": tool_schema_version,
        "policy_version": policy_version,
        "adapter_version": adapter_version,
        "code_revision": code_revision,
        "dataset_version": dataset_version,
        "run_concurrency": str(concurrency),
    }
    if any(not value.strip() for value in metadata.values()):
        raise ValueError("all version metadata fields must be non-empty")

    output_path = output_path.expanduser().resolve()
    if output_path.exists():
        if not resume:
            raise FileExistsError(f"{output_path} already exists; pass --resume to continue")
        existing = json.loads(output_path.read_text(encoding="utf-8"))
        if not isinstance(existing, dict) or existing.get("schema_version") != RUN_SCHEMA_VERSION:
            raise ValueError("existing run file has an unsupported schema")
        if (
            existing.get("dataset_version") != dataset_version
            or existing.get("dataset_fingerprint") != dataset_fingerprint
            or existing.get("metadata") != metadata
        ):
            raise ValueError("resume metadata does not match the existing run file")
        if existing.get("split") != split or existing.get("trials_per_case") != trials_per_case:
            raise ValueError("resume split/trial count does not match the existing run file")
        document = existing
        if not isinstance(document.get("runs"), list):
            raise ValueError("existing run file must contain a runs array")
    else:
        document = _empty_run_document(dataset_version, dataset_fingerprint, metadata, split, trials_per_case)

    completed = set()
    for row in document["runs"]:
        if not isinstance(row, dict):
            continue
        execution = row.get("execution")
        if not isinstance(execution, dict) or execution.get("status") != "runner_error":
            completed.add((row.get("case_id"), row.get("trial_index")))
    settings = settings or Settings.from_env()
    provider = _make_provider(provider_name, model, api_key, base_url, timeout_seconds)
    artifact_base = output_path.parent / f"{output_path.stem}-artifacts"
    semaphore = asyncio.Semaphore(concurrency)
    write_lock = asyncio.Lock()

    async def execute_trial(case: dict[str, Any], trial_index: int) -> None:
        if (case["case_id"], trial_index) in completed:
            return
        run_id = uuid4().hex
        started_at = _utc_now()
        artifact_root = artifact_base / _safe_case_id(case["case_id"]) / f"trial-{trial_index}"
        async with semaphore:
            try:
                execution = await _run_one_case(
                    case,
                    provider=provider,
                    settings=settings,
                    artifact_root=artifact_root,
                    browser_enabled=browser_enabled,
                    max_steps=max_steps,
                )
            except Exception as exc:  # noqa: BLE001 - keep a failed trial in the evaluation record.
                execution = {
                    "status": "runner_error",
                    "reason": type(exc).__name__,
                    "error": str(exc)[:500],
                    "duration_seconds": None,
                    "physical_requests": None,
                    "tool_actions": None,
                    "tokens_input": None,
                    "tokens_output": None,
                    "research": None,
                    "trace": [],
                }
        run_row = {
            "run_id": run_id,
            "case_id": case["case_id"],
            "trial_index": trial_index,
            "started_at": started_at,
            "completed_at": _utc_now(),
            "artifact_root": str(artifact_root),
            "execution": execution,
        }
        async with write_lock:
            existing_index = next(
                (
                    index
                    for index, row in enumerate(document["runs"])
                    if isinstance(row, dict)
                    and row.get("case_id") == case["case_id"]
                    and row.get("trial_index") == trial_index
                ),
                None,
            )
            if existing_index is None:
                document["runs"].append(run_row)
            else:
                document["runs"][existing_index] = run_row
            document["runs"].sort(
                key=lambda row: (
                    str(row.get("case_id", "")) if isinstance(row, dict) else "",
                    int(row.get("trial_index", 0)) if isinstance(row, dict) else 0,
                )
            )
            _atomic_write_json(output_path, document)

    try:
        await asyncio.gather(
            *(execute_trial(case, trial_index) for case in selected for trial_index in range(1, trials_per_case + 1))
        )
    finally:
        await provider.close()

    document["updated_at"] = _utc_now()
    _atomic_write_json(output_path, document)
    return document


def create_research_review_packet(dataset: dict[str, Any], runs_document: dict[str, Any]) -> dict[str, Any]:
    if dataset.get("dataset_version") != runs_document.get("dataset_version"):
        raise ValueError("dataset_version does not match the run file")
    if runs_document.get("dataset_fingerprint") and runs_document.get("dataset_fingerprint") != _document_fingerprint(dataset):
        raise ValueError("dataset contents do not match the run file fingerprint")
    if not isinstance(runs_document.get("runs"), list):
        raise TypeError("run document must contain a runs array")
    cases = {
        case["case_id"]: case
        for case in dataset.get("cases", [])
        if isinstance(case, dict) and isinstance(case.get("case_id"), str)
    }
    packet_rows = []
    seen: set[tuple[str, int]] = set()
    for run in runs_document.get("runs", []):
        if not isinstance(run, dict):
            continue
        case_id = run.get("case_id")
        trial_index = run.get("trial_index")
        case = cases.get(case_id)
        if case is None or not isinstance(trial_index, int):
            continue
        key = (case_id, trial_index)
        if key in seen:
            raise ValueError(f"duplicate run for {case_id} trial {trial_index}")
        seen.add(key)
        execution = run.get("execution") if isinstance(run.get("execution"), dict) else {}
        packet_rows.append({
            "review_id": hashlib.sha256(
                f"{dataset['dataset_version']}:{run.get('run_id', case_id)}:{trial_index}".encode()
            ).hexdigest()[:20],
            "case_id": case_id,
            "trial_index": trial_index,
            "case_context": {
                "field": case.get("field"),
                "topic": case.get("topic"),
                "research_question": case.get("research_question"),
            },
            "execution_status": execution.get("status"),
            "execution_reason": execution.get("reason"),
            "research": execution.get("research"),
            "review": {
                "adjudicated": False,
                "reviewers": [],
                "metrics": {name: None for name in TRIAL_METRICS},
            },
        })
    return {
        "schema_version": REVIEW_SCHEMA_VERSION,
        "dataset_version": dataset["dataset_version"],
        "notice": "Blind review packet. Gold labels and provider/model metadata are intentionally omitted. Review each output independently; do not use the model under review as a grader.",
        "reviews": packet_rows,
    }


def build_research_trial_document(
    dataset: dict[str, Any],
    runs_document: dict[str, Any],
    review_packet: dict[str, Any],
) -> dict[str, Any]:
    versions = dataset.get("dataset_version")
    if versions != runs_document.get("dataset_version") or versions != review_packet.get("dataset_version"):
        raise ValueError("dataset_version must match across dataset, runs, and review packet")
    if runs_document.get("dataset_fingerprint") and runs_document.get("dataset_fingerprint") != _document_fingerprint(dataset):
        raise ValueError("dataset contents do not match the run file fingerprint")
    if review_packet.get("schema_version") != REVIEW_SCHEMA_VERSION:
        raise ValueError("review packet schema_version must be 1")
    metadata = runs_document.get("metadata")
    if not isinstance(metadata, dict) or any(name not in metadata for name in TRIAL_METADATA):
        raise ValueError("run document is missing required version metadata")
    reviews: dict[tuple[str, int], dict[str, Any]] = {}
    for item in review_packet.get("reviews", []):
        if not isinstance(item, dict):
            continue
        key = (item.get("case_id"), item.get("trial_index"))
        if key in reviews:
            raise ValueError(f"duplicate review for {key[0]} trial {key[1]}")
        reviews[key] = item.get("review") if isinstance(item.get("review"), dict) else {}

    output = []
    for run in runs_document.get("runs", []):
        if not isinstance(run, dict) or not isinstance(run.get("case_id"), str) or not isinstance(run.get("trial_index"), int):
            continue
        review = reviews.get((run["case_id"], run["trial_index"]), {})
        evaluation = {
            "adjudicated": review.get("adjudicated") is True,
            "reviewers": review.get("reviewers", []),
        }
        metrics = review.get("metrics", {})
        if not isinstance(metrics, dict):
            metrics = {}
        output.append({
            "case_id": run["case_id"],
            "trial_index": run["trial_index"],
            "metadata": metadata,
            "evaluation": evaluation,
            "metrics": {name: metrics.get(name) for name in TRIAL_METRICS},
            "run_id": run.get("run_id"),
            "execution_status": (run.get("execution") or {}).get("status"),
            "execution_reason": (run.get("execution") or {}).get("reason"),
        })
    return {
        "schema_version": 1,
        "dataset_version": versions,
        "source_run_file_created_at": runs_document.get("created_at"),
        "trials": output,
    }
