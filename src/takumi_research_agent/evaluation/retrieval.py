from __future__ import annotations

import math
from collections import Counter
from typing import Any
from urllib.parse import urlsplit


def _adjudicated(item: dict[str, Any], *, name: str) -> list[str]:
    errors = []
    if item.get("adjudicated") is not True:
        errors.append(f"{name} is not human-adjudicated")
    if not isinstance(item.get("reviewers"), list) or not item["reviewers"]:
        errors.append(f"{name} needs reviewer IDs")
    sources = item.get("source_snapshots")
    if not isinstance(sources, list) or not sources:
        errors.append(f"{name} needs source_snapshots")
    else:
        for index, source in enumerate(sources):
            url = urlsplit(source["url"]) if isinstance(source, dict) and isinstance(source.get("url"), str) else None
            digest = source.get("sha256") if isinstance(source, dict) else None
            if url is None or url.scheme != "https" or not url.hostname or url.username or url.password or not isinstance(digest, str) or len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
                errors.append(f"{name}.source_snapshots[{index}] needs url and sha256")
    return errors


def _ndcg(relevance: dict[str, int], ranked_ids: list[str], *, k: int = 20) -> float | None:
    ideal = sorted(relevance.values(), reverse=True)[:k]
    ideal_dcg = sum((2**score - 1) / math.log2(position + 2) for position, score in enumerate(ideal))
    if ideal_dcg == 0:
        return 1.0
    dcg = sum((2 ** relevance.get(item_id, 0) - 1) / math.log2(position + 2) for position, item_id in enumerate(ranked_ids[:k]))
    return dcg / ideal_dcg


def evaluate_retrieval_benchmark(dataset: dict[str, Any], results: dict[str, Any]) -> dict[str, Any]:
    """Score a versioned, human-labeled history retrieval/ranking and dedup benchmark."""
    errors: list[str] = []
    if dataset.get("schema_version") != 1:
        errors.append("dataset schema_version must be 1")
    version = dataset.get("dataset_version")
    if not isinstance(version, str) or not version.strip():
        errors.append("dataset_version is required")
    if results.get("schema_version") != 1 or results.get("dataset_version") != version:
        errors.append("results schema_version/dataset_version must match the dataset")
    metadata = results.get("metadata")
    if not isinstance(metadata, dict) or any(not isinstance(metadata.get(key), str) or not metadata[key].strip() for key in ("retriever_version", "index_version", "code_revision")):
        errors.append("results metadata needs retriever_version, index_version, and code_revision")
    cases = dataset.get("cases")
    if not isinstance(cases, list):
        cases = []
        errors.append("dataset cases must be an array")
    retrieval_cases = [item for item in cases if isinstance(item, dict) and item.get("kind") == "retrieval"]
    ranking_cases = [item for item in cases if isinstance(item, dict) and item.get("kind") == "ranking"]
    dedup_cases = [item for item in cases if isinstance(item, dict) and item.get("kind") == "dedup"]
    if len(dedup_cases) < 40:
        errors.append(f"dedup benchmark has {len(dedup_cases)} pairs; minimum is 40")
    cross_language_pairs = sum(item.get("cross_language") is True for item in dedup_cases)
    if cross_language_pairs < 20:
        errors.append(f"dedup benchmark has {cross_language_pairs} cross-language pairs; minimum is 20")
    if len(retrieval_cases) < 40:
        errors.append(f"retrieval benchmark has {len(retrieval_cases)} queries; minimum is 40")
    if len(ranking_cases) < 40:
        errors.append(f"ranking benchmark has {len(ranking_cases)} queries; minimum is 40")

    by_kind: dict[str, dict[str, dict[str, Any]]] = {"retrieval": {}, "ranking": {}, "dedup": {}}
    for index, case in enumerate(cases):
        if not isinstance(case, dict) or case.get("kind") not in by_kind:
            errors.append(f"cases[{index}] has an unsupported kind")
            continue
        kind = case["kind"]
        case_id = case.get("case_id")
        if not isinstance(case_id, str) or not case_id.strip():
            errors.append(f"cases[{index}].case_id is required")
            continue
        if case_id in by_kind[kind]:
            errors.append(f"duplicate {kind} case_id {case_id!r}")
        by_kind[kind][case_id] = case
        errors.extend(_adjudicated(case, name=f"{kind} case {case_id}"))
        if case.get("split") != "holdout":
            errors.append(f"benchmark case {case_id!r} must be in the holdout split")
        if kind == "retrieval":
            relevant = case.get("relevant_ids")
            if not isinstance(relevant, list) or not relevant or any(not isinstance(item, str) or not item for item in relevant):
                errors.append(f"retrieval case {case_id!r} needs relevant_ids")
            if case.get("field") not in {"technology", "physics", "chemistry", "biology"}:
                errors.append(f"retrieval case {case_id!r} needs a supported field")
            if not isinstance(case.get("corpus_size"), int) or isinstance(case["corpus_size"], bool) or case["corpus_size"] < 201:
                errors.append(f"retrieval case {case_id!r} needs at least 200 distractors in corpus_size")
        elif kind == "ranking":
            relevance = case.get("relevance")
            if not isinstance(relevance, dict) or not relevance or any(not isinstance(key, str) or not key or not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= 3 for key, value in relevance.items()):
                errors.append(f"ranking case {case_id!r} needs relevance labels from 0 to 3")
            if case.get("field") not in {"technology", "physics", "chemistry", "biology"}:
                errors.append(f"ranking case {case_id!r} needs a supported field")
        else:
            if not isinstance(case.get("gold_duplicate"), bool):
                errors.append(f"dedup case {case_id!r} needs a boolean gold_duplicate label")
            if not isinstance(case.get("cross_language"), bool):
                errors.append(f"dedup case {case_id!r} needs a boolean cross_language label")

    outputs = results.get("cases")
    if not isinstance(outputs, list):
        outputs = []
        errors.append("results cases must be an array")
    result_map: dict[tuple[str, str], dict[str, Any]] = {}
    for index, item in enumerate(outputs):
        if not isinstance(item, dict) or item.get("kind") not in by_kind or not isinstance(item.get("case_id"), str):
            errors.append(f"results cases[{index}] needs a valid kind and case_id")
            continue
        key = (item["kind"], item["case_id"])
        if item["case_id"] not in by_kind[item["kind"]]:
            errors.append(f"result references unknown {item['kind']} case {item['case_id']!r}")
        if key in result_map:
            errors.append(f"duplicate result for {key[0]} case {key[1]!r}")
        result_map[key] = item

    retrieval_recalls: list[float] = []
    for case_id, case in by_kind["retrieval"].items():
        output = result_map.get(("retrieval", case_id))
        if output is None or not isinstance(output.get("ranked_ids"), list) or any(not isinstance(item, str) for item in output["ranked_ids"]):
            errors.append(f"missing ranked_ids for retrieval case {case_id!r}")
            continue
        relevant = set(case.get("relevant_ids", []))
        if relevant:
            retrieval_recalls.append(len(relevant.intersection(output["ranked_ids"][:20])) / len(relevant))

    ndcgs: list[float] = []
    for case_id, case in by_kind["ranking"].items():
        output = result_map.get(("ranking", case_id))
        relevance = case.get("relevance")
        if output is None or not isinstance(output.get("ranked_ids"), list) or any(not isinstance(item, str) for item in output.get("ranked_ids", [])) or not isinstance(relevance, dict):
            errors.append(f"missing ranked_ids for ranking case {case_id!r}")
            continue
        score = _ndcg(relevance, output["ranked_ids"])
        if score is not None:
            ndcgs.append(score)

    true_positive = false_positive = false_negative = 0
    duplicate_labels = Counter()
    for case_id, case in by_kind["dedup"].items():
        output = result_map.get(("dedup", case_id))
        if output is None or not isinstance(output.get("predicted_duplicate"), bool) or not isinstance(case.get("gold_duplicate"), bool):
            errors.append(f"missing predicted_duplicate for dedup case {case_id!r}")
            continue
        gold, predicted = case["gold_duplicate"], output["predicted_duplicate"]
        duplicate_labels[gold] += 1
        true_positive += int(gold and predicted)
        false_positive += int(not gold and predicted)
        false_negative += int(gold and not predicted)

    dedup_precision = true_positive / (true_positive + false_positive) if true_positive + false_positive else None
    dedup_recall = true_positive / (true_positive + false_negative) if true_positive + false_negative else None
    recall_at_20 = sum(retrieval_recalls) / len(retrieval_recalls) if retrieval_recalls else None
    mean_ndcg_at_20 = sum(ndcgs) / len(ndcgs) if ndcgs else None
    metrics = {
        "retrieval_recall_at_20": recall_at_20,
        "retrieval_queries_scored": len(retrieval_recalls),
        "ranking_ndcg_at_20": mean_ndcg_at_20,
        "ranking_queries_scored": len(ndcgs),
        "dedup_precision": dedup_precision,
        "dedup_recall": dedup_recall,
        "dedup_true_positive": true_positive,
        "dedup_false_positive": false_positive,
        "dedup_false_negative": false_negative,
        "dedup_pairs_scored": sum(1 for (kind, case_id) in result_map if kind == "dedup" and case_id in by_kind["dedup"]),
    }
    gates = {
        "retrieval_recall_at_20_at_least_95_percent": recall_at_20 is not None and recall_at_20 >= 0.95,
        "duplicate_verdict_precision_at_least_95_percent": dedup_precision is not None and dedup_precision >= 0.95,
        "duplicate_verdict_recall_at_least_95_percent": dedup_recall is not None and dedup_recall >= 0.95,
    }
    complete = not errors and len(retrieval_recalls) == len(retrieval_cases) and len(ndcgs) == len(ranking_cases)
    # Empty all-negative dedup sets cannot produce a recall denominator and never qualify.
    complete = complete and duplicate_labels[True] > 0 and duplicate_labels[False] > 0
    passed = complete and all(gates.values())
    return {
        "schema_version": 1,
        "dataset_version": version,
        "status": "qualified" if passed else ("failed" if complete else "incomplete"),
        "qualified": passed,
        "case_counts": {"retrieval": len(retrieval_cases), "ranking": len(ranking_cases), "dedup": len(dedup_cases), "cross_language_dedup": cross_language_pairs},
        "metrics": metrics,
        "gates": gates,
        "errors": sorted(set(errors)),
        "limitations": ["A score is only as reliable as the independently adjudicated labels and the corpus snapshot supplied with the benchmark."],
    }
