from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from takumi_research_agent.config import Settings

from .miracl_import import import_miracl_jsonl
from .qualification import calibrate_reviewers, qualify_research_dataset, read_json
from .research_runner import (
    _atomic_write_json,
    build_research_trial_document,
    create_research_review_packet,
    current_code_revision,
    run_research_trials,
)
from .retrieval import evaluate_retrieval_benchmark
from .retrieval_runner import run_retrieval_dataset
from .semantic_retrieval import run_semantic_retrieval_dataset


def main() -> int:
    parser = argparse.ArgumentParser(description="Run and score versioned Takumi evaluation suites")
    subparsers = parser.add_subparsers(dest="suite", required=True)
    research = subparsers.add_parser("research", help="qualify labeled research/evidence holdout trials")
    research.add_argument("--dataset", required=True)
    research.add_argument("--trials", required=True)
    research.add_argument("--minimum-cases", type=int, default=40, help="minimum human-labeled holdout cases required to qualify (default: 40)")
    research.add_argument("--trials-per-case", type=int, default=3)
    retrieval = subparsers.add_parser("retrieval", help="score labeled history retrieval, ranking, and dedup")
    retrieval.add_argument("--dataset", required=True)
    retrieval.add_argument("--results", required=True)
    miracl_import = subparsers.add_parser(
        "import-miracl-dev",
        help="convert a locally exported Indonesian MIRACL dev JSONL file into a development-only retrieval dataset",
    )
    miracl_import.add_argument("--input-jsonl", required=True, help="JSONL rows with query_id/query/positive_passages/negative_passages")
    miracl_import.add_argument("--output", required=True)
    miracl_import.add_argument("--case-limit", type=int, default=40)
    reviewer = subparsers.add_parser("reviewer", help="measure reviewer agreement against adjudication")
    reviewer.add_argument("--dataset", required=True)
    reviewer.add_argument("--labels", required=True)
    reviewer.add_argument("--minimum-labels", type=int, default=20)

    run_research = subparsers.add_parser(
        "run-research",
        help="run the read-only research agent against development or holdout cases; this makes live model/source requests",
    )
    run_research.add_argument("--dataset", required=True)
    run_research.add_argument("--output", required=True)
    run_research.add_argument("--provider", choices=("openai-compatible", "anthropic"), required=True)
    run_research.add_argument("--model", required=True)
    run_research.add_argument("--api-key-env", help="environment variable containing the provider key")
    run_research.add_argument("--base-url", help="optional OpenAI-compatible or Anthropic API base URL")
    run_research.add_argument("--timeout-seconds", type=float, default=90)
    run_research.add_argument("--split", choices=("development", "holdout", "all"), default="holdout")
    run_research.add_argument("--trials-per-case", type=int, default=3)
    run_research.add_argument("--prompt-version", required=True)
    run_research.add_argument("--tool-schema-version", default="research-tools-v1")
    run_research.add_argument("--policy-version", default="research-policy-v1")
    run_research.add_argument("--adapter-version")
    run_research.add_argument("--code-revision")
    run_research.add_argument("--max-steps", type=int, default=40)
    run_research.add_argument("--concurrency", type=int, default=1, help="maximum concurrent trials (1-10); check provider quotas before increasing")
    run_research.add_argument("--browser", action="store_true", help="enable the local Playwright fallback")
    run_research.add_argument("--resume", action="store_true", help="continue a compatible run file and skip completed trials")

    review_packet = subparsers.add_parser("review-packet", help="create a blind human-review packet from raw research runs")
    review_packet.add_argument("--dataset", required=True)
    review_packet.add_argument("--runs", required=True)
    review_packet.add_argument("--output", required=True)

    build_trials = subparsers.add_parser("build-trials", help="combine human annotations with run metadata for qualification")
    build_trials.add_argument("--dataset", required=True)
    build_trials.add_argument("--runs", required=True)
    build_trials.add_argument("--reviews", required=True)
    build_trials.add_argument("--output", required=True)

    run_retrieval = subparsers.add_parser(
        "run-retrieval",
        help="run the current lexical history retriever on a labeled retrieval/ranking/dedup corpus",
    )
    run_retrieval.add_argument("--dataset", required=True)
    run_retrieval.add_argument("--output", required=True)
    run_retrieval.add_argument("--duplicate-threshold", type=float, default=0.10)
    run_retrieval.add_argument("--code-revision")

    run_semantic = subparsers.add_parser(
        "run-semantic-retrieval",
        help="build an in-memory embedding index and generate semantic retrieval/ranking/dedup predictions",
    )
    run_semantic.add_argument("--dataset", required=True)
    run_semantic.add_argument("--output", required=True)
    run_semantic.add_argument("--embedding-model", required=True)
    run_semantic.add_argument("--api-key-env", default="OPENAI_API_KEY")
    run_semantic.add_argument("--base-url", help="optional OpenAI-compatible embeddings endpoint")
    run_semantic.add_argument("--timeout-seconds", type=float, default=90)
    run_semantic.add_argument("--batch-size", type=int, default=64)
    run_semantic.add_argument("--duplicate-threshold", type=float, default=0.80)
    run_semantic.add_argument("--code-revision")

    args = parser.parse_args()

    if args.suite == "import-miracl-dev":
        try:
            dataset = import_miracl_jsonl(args.input_jsonl, case_limit=args.case_limit)
        except (TypeError, ValueError, OSError) as exc:
            print(json.dumps({"status": "incomplete", "error": str(exc)}, ensure_ascii=False, indent=2))
            return 2
        _atomic_write_json(Path(args.output), dataset)
        case_counts = {kind: sum(case["kind"] == kind for case in dataset["cases"]) for kind in ("retrieval", "ranking")}
        print(json.dumps({
            "status": "development_dataset_created",
            "dataset_version": dataset["dataset_version"],
            "corpus_documents": len(dataset["corpus"]),
            "case_counts": case_counts,
            "output": str(Path(args.output).expanduser().resolve()),
            "qualification": "development-only; this dataset cannot qualify a release",
        }, ensure_ascii=False, indent=2))
        return 0

    if args.suite == "run-research":
        settings = Settings.from_env()
        key_name = args.api_key_env or (
            "ANTHROPIC_API_KEY" if args.provider == "anthropic" else "OPENAI_API_KEY"
        )
        api_key = os.getenv(key_name, "")
        if not api_key:
            parser.error(f"environment variable {key_name} is required for this provider")
        try:
            code_revision = args.code_revision or current_code_revision()
            adapter_version = args.adapter_version or (
                "anthropic-native-v1" if args.provider == "anthropic" else "openai-compatible-v1"
            )
            document = asyncio.run(
                run_research_trials(
                    read_json(args.dataset),
                    output_path=Path(args.output),
                    split=args.split,
                    trials_per_case=args.trials_per_case,
                    provider_name=args.provider,
                    model=args.model,
                    api_key=api_key,
                    base_url=args.base_url,
                    timeout_seconds=args.timeout_seconds,
                    prompt_version=args.prompt_version,
                    tool_schema_version=args.tool_schema_version,
                    policy_version=args.policy_version,
                    adapter_version=adapter_version,
                    code_revision=code_revision,
                    browser_enabled=args.browser,
                    max_steps=args.max_steps,
                    concurrency=args.concurrency,
                    resume=args.resume,
                    settings=settings,
                )
            )
        except (ValueError, FileExistsError, OSError) as exc:
            print(json.dumps({"status": "incomplete", "error": str(exc)}, ensure_ascii=False, indent=2))
            return 2
        status_counts: dict[str, int] = {}
        for row in document["runs"]:
            status = row.get("execution", {}).get("status", "unknown")
            status_counts[status] = status_counts.get(status, 0) + 1
        incomplete = sum(count for status, count in status_counts.items() if status != "completed")
        print(json.dumps({
            "status": "completed_with_incomplete_runs" if incomplete else "runs_saved",
            "dataset_version": document["dataset_version"],
            "cases": len({row["case_id"] for row in document["runs"]}),
            "trials": len(document["runs"]),
            "execution_statuses": status_counts,
            "output": str(Path(args.output).expanduser().resolve()),
        }, ensure_ascii=False, indent=2))
        return 2 if incomplete else 0

    if args.suite == "review-packet":
        packet = create_research_review_packet(read_json(args.dataset), read_json(args.runs))
        _atomic_write_json(Path(args.output), packet)
        print(json.dumps({"status": "created", "reviews": len(packet["reviews"]), "output": args.output}, indent=2))
        return 0

    if args.suite == "build-trials":
        trials = build_research_trial_document(read_json(args.dataset), read_json(args.runs), read_json(args.reviews))
        _atomic_write_json(Path(args.output), trials)
        print(json.dumps({"status": "created", "trials": len(trials["trials"]), "output": args.output}, indent=2))
        return 0

    if args.suite == "run-retrieval":
        try:
            report = run_retrieval_dataset(
                read_json(args.dataset),
                duplicate_threshold=args.duplicate_threshold,
                code_revision=args.code_revision,
            )
        except (ValueError, OSError) as exc:
            print(json.dumps({"status": "incomplete", "error": str(exc)}, ensure_ascii=False, indent=2))
            return 2
        _atomic_write_json(Path(args.output), report)
        print(json.dumps({
            "status": "results_generated",
            "dataset_version": report["dataset_version"],
            "cases": len(report["cases"]),
            "retriever_version": report["metadata"]["retriever_version"],
            "output": str(Path(args.output).expanduser().resolve()),
        }, ensure_ascii=False, indent=2))
        return 0

    if args.suite == "run-semantic-retrieval":
        api_key = os.getenv(args.api_key_env, "")
        if not api_key:
            parser.error(f"environment variable {args.api_key_env} is required for this provider")
        try:
            report = asyncio.run(
                run_semantic_retrieval_dataset(
                    read_json(args.dataset),
                    api_key=api_key,
                    model=args.embedding_model,
                    base_url=args.base_url,
                    timeout_seconds=args.timeout_seconds,
                    batch_size=args.batch_size,
                    duplicate_threshold=args.duplicate_threshold,
                    code_revision=args.code_revision,
                )
            )
        except Exception as exc:  # noqa: BLE001 - report provider errors through the CLI without a traceback.
            print(json.dumps({"status": "incomplete", "error": f"{type(exc).__name__}: {str(exc)[:500]}"}, ensure_ascii=False, indent=2))
            return 2
        _atomic_write_json(Path(args.output), report)
        print(json.dumps({
            "status": "results_generated",
            "dataset_version": report["dataset_version"],
            "cases": len(report["cases"]),
            "retriever_version": report["metadata"]["retriever_version"],
            "embedding_model": report["metadata"]["embedding_model"],
            "output": str(Path(args.output).expanduser().resolve()),
        }, ensure_ascii=False, indent=2))
        return 0

    dataset = read_json(args.dataset)
    if args.suite == "research":
        report = qualify_research_dataset(
            dataset,
            read_json(args.trials),
            minimum_cases=args.minimum_cases,
            trials_per_case=args.trials_per_case,
        )
        exit_code = 0 if report["qualified"] else 2
    elif args.suite == "retrieval":
        report = evaluate_retrieval_benchmark(dataset, read_json(args.results))
        exit_code = 0 if report["status"] in {"qualified", "development_scored"} else 2
    else:
        labels_document = read_json(args.labels)
        adjudicated = {
            case["case_id"]: case["adjudication"]["decision"]
            for case in dataset.get("cases", [])
            if isinstance(case, dict)
            and isinstance(case.get("case_id"), str)
            and isinstance(case.get("adjudication"), dict)
            and isinstance(case["adjudication"].get("decision"), str)
        }
        report = calibrate_reviewers(adjudicated, labels_document.get("labels", []), minimum_labels_per_reviewer=args.minimum_labels)
        exit_code = 0 if report["status"] == "measured" else 2
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
