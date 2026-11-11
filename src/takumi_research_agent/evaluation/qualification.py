from __future__ import annotations

import json
import math
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

FIELDS = {"technology", "physics", "chemistry", "biology"}
DECISIONS = {"pass", "fail", "uncertain"}
TRIAL_METADATA = (
    "provider",
    "model",
    "prompt_version",
    "tool_schema_version",
    "policy_version",
    "adapter_version",
    "code_revision",
    "dataset_version",
)
TRIAL_METRICS = (
    "critical_unsupported_claims",
    "contradicted_critical_claims",
    "fabricated_sources",
    "fabricated_quotes",
    "journal_policy_bypasses",
    "supported_citations",
    "citation_total",
    "numeric_date_unit_parity_failures",
    "numeric_date_unit_parity_checks",
    "reader_score",
)


def _is_nonnegative_metric(name: str, value: Any) -> bool:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        return False
    return 1 <= value <= 5 if name == "reader_score" else True


def _has_reviewer_ids(value: Any) -> bool:
    return (
        isinstance(value, list)
        and bool(value)
        and all(isinstance(reviewer, str) and reviewer.strip() for reviewer in value)
        and len(set(value)) == len(value)
    )


def read_json(path: str | Path) -> dict[str, Any]:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError("Evaluation input must be a JSON object")
    return value


def _dataset_errors(dataset: dict[str, Any], *, minimum_cases: int = 40) -> tuple[list[str], list[dict[str, Any]]]:
    errors: list[str] = []
    if dataset.get("schema_version") != 1:
        errors.append("dataset schema_version must be 1")
    version = dataset.get("dataset_version")
    if not isinstance(version, str) or not version.strip():
        errors.append("dataset_version is required")
    cases = dataset.get("cases")
    if not isinstance(cases, list):
        return errors + ["cases must be an array"], []
    if len(cases) < minimum_cases:
        errors.append(f"dataset has {len(cases)} cases; minimum is {minimum_cases}")
    ids: set[str] = set()
    counts = Counter()
    splits = set()
    for index, case in enumerate(cases):
        if not isinstance(case, dict):
            errors.append(f"cases[{index}] must be an object")
            continue
        case_id = case.get("case_id")
        if not isinstance(case_id, str) or not case_id.strip():
            errors.append(f"cases[{index}].case_id is required")
        elif case_id in ids:
            errors.append(f"duplicate case_id: {case_id}")
        else:
            ids.add(case_id)
        field = case.get("field")
        if field not in FIELDS:
            errors.append(f"cases[{index}].field must be one of the four supported fields")
        else:
            counts[field] += 1
        if case.get("split") not in {"development", "holdout"}:
            errors.append(f"cases[{index}].split must be development or holdout")
        else:
            splits.add(case["split"])
        sources = case.get("source_snapshots")
        if not isinstance(sources, list) or not sources:
            errors.append(f"cases[{index}] needs at least one source snapshot")
        else:
            for source_index, source in enumerate(sources):
                try:
                    parsed_url = urlsplit(source["url"]) if isinstance(source, dict) and isinstance(source.get("url"), str) else None
                    valid_host = parsed_url is not None and bool(parsed_url.hostname)
                except ValueError:
                    parsed_url, valid_host = None, False
                if source is None or not isinstance(source, dict) or parsed_url is None or parsed_url.scheme != "https" or not valid_host or parsed_url.username or parsed_url.password or not isinstance(source.get("sha256"), str) or not re.fullmatch(r"[a-f0-9]{64}", source["sha256"]):
                    errors.append(f"cases[{index}].source_snapshots[{source_index}] needs a URL and 64-character sha256")
        adjudication = case.get("adjudication")
        if not isinstance(adjudication, dict) or adjudication.get("adjudicated") is not True:
            errors.append(f"cases[{index}] needs a human adjudication")
        else:
            if not _has_reviewer_ids(adjudication.get("reviewers")):
                errors.append(f"cases[{index}].adjudication needs reviewer IDs")
            if adjudication.get("decision") not in {"pass", "fail", "uncertain"}:
                errors.append(f"cases[{index}].adjudication.decision must be pass, fail, or uncertain")
            elif case.get("split") == "holdout" and adjudication["decision"] == "uncertain":
                errors.append(f"cases[{index}] with uncertain adjudication cannot be used in holdout")
        gold = case.get("gold")
        if not isinstance(gold, dict):
            errors.append(f"cases[{index}].gold is required")
        else:
            for metric in TRIAL_METRICS:
                value = gold.get(metric)
                if not _is_nonnegative_metric(metric, value):
                    expected = "an integer from 1 to 5" if metric == "reader_score" else "a non-negative integer"
                    errors.append(f"cases[{index}].gold.{metric} must be {expected}")
            if all(isinstance(gold.get(name), int) and not isinstance(gold.get(name), bool) for name in ("supported_citations", "citation_total")) and gold["supported_citations"] > gold["citation_total"]:
                errors.append(f"cases[{index}].gold.supported_citations cannot exceed citation_total")
            if all(isinstance(gold.get(name), int) and not isinstance(gold.get(name), bool) for name in ("numeric_date_unit_parity_failures", "numeric_date_unit_parity_checks")) and gold["numeric_date_unit_parity_failures"] > gold["numeric_date_unit_parity_checks"]:
                errors.append(f"cases[{index}].gold parity failures cannot exceed parity checks")
    for field in sorted(FIELDS):
        if counts[field] < 10:
            errors.append(f"field {field} has {counts[field]} cases; minimum is 10")
    if not {"development", "holdout"}.issubset(splits):
        errors.append("dataset must contain development and holdout splits")
    return errors, [case for case in cases if isinstance(case, dict)]


def _validate_trial(trial: dict[str, Any], *, case_ids: set[str], dataset_version: str) -> list[str]:
    errors = []
    case_id = trial.get("case_id")
    execution_status = trial.get("execution_status")
    if execution_status != "completed":
        errors.append(f"trial {case_id!r} did not produce a completed research bundle")
    if case_id not in case_ids:
        errors.append(f"trial references unknown case_id {case_id!r}")
    metadata = trial.get("metadata")
    if not isinstance(metadata, dict):
        return errors + [f"trial {case_id!r} needs metadata"]
    for name in TRIAL_METADATA:
        if not isinstance(metadata.get(name), str) or not metadata[name].strip():
            errors.append(f"trial {case_id!r} metadata.{name} is required")
    if metadata.get("dataset_version") != dataset_version:
        errors.append(f"trial {case_id!r} uses a different dataset_version")
    evaluation = trial.get("evaluation")
    if not isinstance(evaluation, dict) or evaluation.get("adjudicated") is not True or not _has_reviewer_ids(evaluation.get("reviewers")):
        errors.append(f"trial {case_id!r} needs independent human evaluation and reviewer IDs")
    metrics = trial.get("metrics")
    if not isinstance(metrics, dict):
        return errors + [f"trial {case_id!r} needs metrics"]
    for name in TRIAL_METRICS:
        value = metrics.get(name)
        if not _is_nonnegative_metric(name, value):
            expected = "an integer from 1 to 5" if name == "reader_score" else "a non-negative integer"
            errors.append(f"trial {case_id!r} metrics.{name} must be {expected}")
    if all(isinstance(metrics.get(name), (int, float)) for name in ("supported_citations", "citation_total")) and metrics["supported_citations"] > metrics["citation_total"]:
        errors.append(f"trial {case_id!r} supported_citations cannot exceed citation_total")
    if all(isinstance(metrics.get(name), (int, float)) for name in ("numeric_date_unit_parity_failures", "numeric_date_unit_parity_checks")) and metrics["numeric_date_unit_parity_failures"] > metrics["numeric_date_unit_parity_checks"]:
        errors.append(f"trial {case_id!r} parity failures cannot exceed parity checks")
    return errors


def qualify_research_dataset(
    dataset: dict[str, Any],
    trials_document: dict[str, Any],
    *,
    minimum_cases: int = 40,
    trials_per_case: int = 3,
) -> dict[str, Any]:
    """Evaluate input completeness and apply the initial research qualification gates.

    Source truth is not inferred here: each dataset case must arrive with human adjudication
    and immutable source snapshots. This function validates the evidence trail and scores
    model-generated trial summaries supplied by an evaluator.
    """
    errors, cases = _dataset_errors(dataset, minimum_cases=minimum_cases)
    if minimum_cases < 1:
        errors.append("minimum_cases must be positive")
    if trials_per_case < 1:
        errors.append("trials_per_case must be positive")
    dataset_version = dataset.get("dataset_version")
    holdout_ids = {case.get("case_id") for case in cases if case.get("split") == "holdout" and isinstance(case.get("case_id"), str)}
    holdout_counts = Counter(
        case.get("field") if isinstance(case.get("field"), str) else None
        for case in cases
        if case.get("split") == "holdout"
    )
    if len(holdout_ids) < minimum_cases:
        errors.append(f"holdout has {len(holdout_ids)} cases; minimum is {minimum_cases}")
    for field in sorted(FIELDS):
        if holdout_counts[field] < 10:
            errors.append(f"holdout field {field} has {holdout_counts[field]} cases; minimum is 10")
    case_ids = {case.get("case_id") for case in cases if isinstance(case.get("case_id"), str)}
    trials = trials_document.get("trials")
    if trials_document.get("schema_version") != 1:
        errors.append("trial-results schema_version must be 1")
    if trials_document.get("dataset_version") != dataset_version:
        errors.append("trial-results dataset_version does not match the dataset")
    if not isinstance(trials, list):
        errors.append("trials must be an array")
        trials = []
    valid_trials: list[dict[str, Any]] = []
    trial_keys: set[tuple[str, int]] = set()
    metadata_signatures: set[tuple[str, ...]] = set()
    for index, trial in enumerate(trials):
        if not isinstance(trial, dict):
            errors.append(f"trials[{index}] must be an object")
            continue
        errors.extend(_validate_trial(trial, case_ids=case_ids, dataset_version=str(dataset_version or "")))
        trial_index = trial.get("trial_index")
        case_id = trial.get("case_id")
        if not isinstance(trial_index, int) or isinstance(trial_index, bool) or trial_index < 1:
            errors.append(f"trial {case_id!r} trial_index must be a positive integer")
        elif isinstance(case_id, str):
            key = (case_id, trial_index)
            if key in trial_keys:
                errors.append(f"duplicate trial_index {trial_index} for case {case_id}")
            trial_keys.add(key)
        metadata = trial.get("metadata")
        if isinstance(metadata, dict) and all(isinstance(metadata.get(name), str) and metadata[name].strip() for name in TRIAL_METADATA):
            metadata_signatures.add(tuple(metadata[name] for name in TRIAL_METADATA))
        valid_trials.append(trial)
    if len(metadata_signatures) > 1:
        errors.append("trials mix provider/model/prompt/tool/policy/adapter/code/dataset versions")

    counts = Counter(trial.get("case_id") for trial in valid_trials if isinstance(trial.get("case_id"), str))
    missing_trials = {case_id: trials_per_case - counts[case_id] for case_id in sorted(holdout_ids) if counts[case_id] < trials_per_case}
    if missing_trials:
        errors.append(f"holdout cases need {trials_per_case} trials each; {len(missing_trials)} case(s) are short")
    holdout_trials = [trial for trial in valid_trials if trial.get("case_id") in holdout_ids and isinstance(trial.get("metrics"), dict)]
    metric_errors = [
        trial
        for trial in holdout_trials
        if any(not _is_nonnegative_metric(name, trial["metrics"].get(name)) for name in TRIAL_METRICS)
    ]
    if metric_errors:
        holdout_trials = [trial for trial in holdout_trials if trial not in metric_errors]

    citation_total = sum(trial["metrics"]["citation_total"] for trial in holdout_trials)
    citation_supported = sum(trial["metrics"]["supported_citations"] for trial in holdout_trials)
    parity_checks = sum(trial["metrics"]["numeric_date_unit_parity_checks"] for trial in holdout_trials)
    parity_failures = sum(trial["metrics"]["numeric_date_unit_parity_failures"] for trial in holdout_trials)
    readable = sum(trial["metrics"]["reader_score"] >= 4 for trial in holdout_trials)
    total_trials = len(holdout_trials)
    by_case: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for trial in holdout_trials:
        by_case[trial["case_id"]].append(trial)
    worst_by_case = []
    for case_id, case_trials in sorted(by_case.items()):
        citations = [
            metrics["supported_citations"] / metrics["citation_total"]
            for trial in case_trials
            if (metrics := trial["metrics"])["citation_total"] > 0
        ]
        worst_by_case.append({
            "case_id": case_id,
            "trials": len(case_trials),
            "worst_citation_support_precision": min(citations) if citations else None,
            "worst_reader_score": min(trial["metrics"]["reader_score"] for trial in case_trials),
            "critical_claim_violations": max(
                trial["metrics"]["critical_unsupported_claims"] + trial["metrics"]["contradicted_critical_claims"]
                for trial in case_trials
            ),
            "source_or_policy_bypasses": max(
                trial["metrics"]["fabricated_sources"] + trial["metrics"]["fabricated_quotes"] + trial["metrics"]["journal_policy_bypasses"]
                for trial in case_trials
            ),
            "numeric_date_unit_parity_failures": max(trial["metrics"]["numeric_date_unit_parity_failures"] for trial in case_trials),
            "reader_score_range": [
                min(trial["metrics"]["reader_score"] for trial in case_trials),
                max(trial["metrics"]["reader_score"] for trial in case_trials),
            ],
        })
    precision_values = [item["worst_citation_support_precision"] for item in worst_by_case if item["worst_citation_support_precision"] is not None]
    gates = {
        "critical_unsupported_or_contradicted_claims_zero": sum(
            trial["metrics"]["critical_unsupported_claims"] + trial["metrics"]["contradicted_critical_claims"]
            for trial in holdout_trials
        ) == 0,
        "fabricated_sources_quotes_and_policy_bypasses_zero": sum(
            trial["metrics"]["fabricated_sources"] + trial["metrics"]["fabricated_quotes"] + trial["metrics"]["journal_policy_bypasses"]
            for trial in holdout_trials
        ) == 0,
        "citation_support_precision_at_least_95_percent": citation_total > 0 and citation_supported / citation_total >= 0.95,
        "numeric_date_unit_parity_100_percent": parity_checks > 0 and parity_failures == 0,
        "reader_score_4_or_more_in_at_least_80_percent": total_trials > 0 and readable / total_trials >= 0.80,
    }
    complete = not errors and not missing_trials and bool(holdout_ids) and bool(holdout_trials)
    passed = complete and all(gates.values())
    return {
        "schema_version": 1,
        "dataset_version": dataset_version,
        "status": "qualified" if passed else ("failed" if complete else "incomplete"),
        "qualified": passed,
        "minimum_cases": minimum_cases,
        "trials_per_case": trials_per_case,
        "dataset_cases": len(cases),
        "holdout_cases": len(holdout_ids),
        "holdout_trials": total_trials,
        "missing_trials_by_case": missing_trials,
        "configuration_signatures": len(metadata_signatures),
        "configuration_manifest": next(
            (trial["metadata"] for trial in valid_trials if isinstance(trial.get("metadata"), dict)),
            None,
        ),
        "metrics": {
            "citation_support_precision": citation_supported / citation_total if citation_total else None,
            "citation_supported": citation_supported,
            "citation_total": citation_total,
            "numeric_date_unit_parity_failures": parity_failures,
            "numeric_date_unit_parity_checks": parity_checks,
            "reader_score_4_plus_rate": readable / total_trials if total_trials else None,
            "worst_case_citation_support_precision": min(precision_values) if precision_values else None,
        },
        "gates": gates,
        "worst_case_by_holdout_case": worst_by_case,
        "errors": sorted(set(errors)),
        "limitations": [
            "The evaluator checks supplied human labels and trial summaries; it does not independently verify source truth or grader quality.",
            "A qualified result applies only to the reported dataset, provider/model, prompts, tools, policy, adapter, and code revision.",
        ],
    }


def _cohen_kappa(gold: list[str], judged: list[str]) -> float | None:
    if not gold:
        return None
    observed = sum(left == right for left, right in zip(gold, judged)) / len(gold)
    gold_counts = Counter(gold)
    judged_counts = Counter(judged)
    labels = set(gold_counts) | set(judged_counts)
    expected = sum(gold_counts[label] * judged_counts[label] for label in labels) / (len(gold) ** 2)
    if math.isclose(expected, 1.0):
        return 1.0 if math.isclose(observed, 1.0) else 0.0
    return (observed - expected) / (1 - expected)


def calibrate_reviewers(
    adjudicated_cases: dict[str, str],
    labels: list[dict[str, Any]],
    *,
    minimum_labels_per_reviewer: int = 20,
) -> dict[str, Any]:
    """Report reviewer agreement against independent adjudicated labels."""
    grouped: dict[str, dict[str, str]] = defaultdict(dict)
    errors: list[str] = []
    valid_adjudications: dict[str, str] = {}
    for case_id, decision in adjudicated_cases.items():
        if not isinstance(case_id, str) or not case_id.strip() or not isinstance(decision, str) or decision not in DECISIONS:
            errors.append("adjudicated cases need non-empty IDs and pass/fail/uncertain decisions")
            continue
        valid_adjudications[case_id] = decision
    for index, item in enumerate(labels):
        if not isinstance(item, dict):
            errors.append(f"labels[{index}] must be an object")
            continue
        case_id, reviewer, decision = item.get("case_id"), item.get("reviewer_id"), item.get("decision")
        if not isinstance(case_id, str) or case_id not in valid_adjudications or not isinstance(reviewer, str) or not reviewer.strip() or not isinstance(decision, str) or not decision.strip():
            errors.append(f"labels[{index}] has an unknown case or missing reviewer/decision")
            continue
        if decision not in DECISIONS:
            errors.append(f"labels[{index}].decision must be pass, fail, or uncertain")
            continue
        if case_id in grouped[reviewer]:
            errors.append(f"reviewer {reviewer!r} labeled case {case_id!r} more than once")
            continue
        grouped[reviewer][case_id] = decision
    reviewers = {}
    for reviewer, results in sorted(grouped.items()):
        common = sorted(set(results) & set(valid_adjudications))
        gold = [valid_adjudications[case_id] for case_id in common]
        judged = [results[case_id] for case_id in common]
        correct = sum(left == right for left, right in zip(gold, judged))
        reviewers[reviewer] = {
            "labels": len(common),
            "accuracy": correct / len(common) if common else None,
            "cohen_kappa_vs_adjudication": _cohen_kappa(gold, judged),
            "minimum_label_count_met": len(common) >= minimum_labels_per_reviewer,
        }
    complete = bool(reviewers) and not errors and all(value["minimum_label_count_met"] for value in reviewers.values())
    return {
        "schema_version": 1,
        "status": "measured" if complete else "incomplete",
        "minimum_labels_per_reviewer": minimum_labels_per_reviewer,
        "reviewers": reviewers,
        "errors": errors,
        "note": "Agreement is reported against independent adjudication; no automatic reviewer approval threshold is assumed.",
    }
