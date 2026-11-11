import hashlib
import json

from takumi_research_agent.evaluation.qualification import (
    calibrate_reviewers,
    qualify_research_dataset,
    read_json,
)
from takumi_research_agent.evaluation.retrieval import evaluate_retrieval_benchmark


def _source_snapshot(case_id: str) -> dict[str, str]:
    return {"url": f"https://example.org/sources/{case_id}", "sha256": "a" * 64}


def _research_case(case_id: str, field: str, split: str) -> dict:
    return {
        "case_id": case_id,
        "split": split,
        "field": field,
        "source_snapshots": [_source_snapshot(case_id)],
        "adjudication": {"adjudicated": True, "reviewers": ["reviewer-a", "reviewer-b"], "decision": "pass"},
        "gold": {
            "critical_unsupported_claims": 0,
            "contradicted_critical_claims": 0,
            "fabricated_sources": 0,
            "fabricated_quotes": 0,
            "journal_policy_bypasses": 0,
            "supported_citations": 19,
            "citation_total": 20,
            "numeric_date_unit_parity_failures": 0,
            "numeric_date_unit_parity_checks": 4,
            "reader_score": 4,
        },
    }


def _research_metrics() -> dict[str, int]:
    return {
        "critical_unsupported_claims": 0,
        "contradicted_critical_claims": 0,
        "fabricated_sources": 0,
        "fabricated_quotes": 0,
        "journal_policy_bypasses": 0,
        "supported_citations": 19,
        "citation_total": 20,
        "numeric_date_unit_parity_failures": 0,
        "numeric_date_unit_parity_checks": 4,
        "reader_score": 4,
    }


def _trial(case_id: str, trial_index: int = 1) -> dict:
    return {
        "case_id": case_id,
        "trial_index": trial_index,
        "execution_status": "completed",
        "metadata": {
            "provider": "provider-v1",
            "model": "model-v1",
            "prompt_version": "prompt-v1",
            "tool_schema_version": "tools-v1",
            "policy_version": "policy-v1",
            "adapter_version": "adapter-v1",
            "code_revision": "revision-v1",
            "dataset_version": "research-v1",
        },
        "evaluation": {"adjudicated": True, "reviewers": ["independent-reviewer"]},
        "metrics": _research_metrics(),
    }


def _retrieval_benchmark() -> tuple[dict, dict]:
    cases = []
    results = []
    corpus = []
    fields = ("technology", "physics", "chemistry", "biology")
    for index in range(40):
        case_id = f"retrieval-{index:02d}"
        corpus.append({"id": f"relevant-{index}", "title": f"Relevant science article {index}"})
        cases.append({
            "case_id": case_id,
            "kind": "retrieval",
            "split": "holdout",
            "field": fields[index % len(fields)],
            "adjudicated": True,
            "reviewers": ["reviewer-a", "reviewer-b"],
            "source_snapshots": [_source_snapshot(case_id)],
            "relevant_ids": [f"relevant-{index}"],
            "corpus_size": 201,
        })
        results.append({"kind": "retrieval", "case_id": case_id, "ranked_ids": [f"relevant-{index}"]})

        case_id = f"ranking-{index:02d}"
        corpus.extend([
            {"id": f"best-{index}", "title": f"Best ranking article {index}"},
            {"id": f"other-{index}", "title": f"Other ranking article {index}"},
        ])
        cases.append({
            "case_id": case_id,
            "kind": "ranking",
            "split": "holdout",
            "field": fields[index % len(fields)],
            "adjudicated": True,
            "reviewers": ["reviewer-a", "reviewer-b"],
            "source_snapshots": [_source_snapshot(case_id)],
            "relevance": {f"best-{index}": 3, f"other-{index}": 0},
        })
        results.append({"kind": "ranking", "case_id": case_id, "ranked_ids": [f"best-{index}", f"other-{index}"]})

        case_id = f"dedup-{index:02d}"
        cross_language = index < 20
        corpus.extend([
            {"id": f"left-{index}", "title": f"Original article {index}", "language": "en"},
            {"id": f"right-{index}", "title": f"Duplicate article {index}", "language": "id" if cross_language else "en"},
        ])
        gold_duplicate = index % 2 == 0
        cases.append({
            "case_id": case_id,
            "kind": "dedup",
            "split": "holdout",
            "adjudicated": True,
            "reviewers": ["reviewer-a", "reviewer-b"],
            "source_snapshots": [_source_snapshot(case_id)],
            "left_id": f"left-{index}",
            "right_id": f"right-{index}",
            "gold_duplicate": gold_duplicate,
            "cross_language": cross_language,
        })
        results.append({"kind": "dedup", "case_id": case_id, "predicted_duplicate": gold_duplicate})
    corpus.extend({"id": f"distractor-{index}", "title": f"Unrelated distractor article {index}"} for index in range(201))
    for case in cases:
        if case["kind"] == "retrieval":
            case["corpus_size"] = len(corpus)
    dataset = {"schema_version": 1, "dataset_version": "retrieval-v1", "corpus": corpus, "cases": cases}
    result_document = {
        "schema_version": 1,
        "dataset_version": "retrieval-v1",
        "dataset_fingerprint": hashlib.sha256(
            json.dumps(dataset, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        ).hexdigest(),
        "metadata": {"retriever_version": "retriever-v1", "index_version": "index-v1", "code_revision": "revision-v1"},
        "cases": results,
    }
    return dataset, result_document


def test_research_qualification_requires_real_labeled_cases_and_trials():
    dataset = {"schema_version": 1, "dataset_version": "research-v1", "cases": []}
    report = qualify_research_dataset(dataset, {"schema_version": 1, "dataset_version": "research-v1", "trials": []})
    assert report["status"] == "incomplete"
    assert not report["qualified"]
    assert report["holdout_trials"] == 0


def test_read_json_rejects_non_object_evaluation_documents(tmp_path):
    path = tmp_path / "invalid-evaluation.json"
    path.write_text("[]", encoding="utf-8")
    try:
        read_json(path)
    except TypeError as exc:
        assert str(exc) == "Evaluation input must be a JSON object"
    else:
        raise AssertionError("non-object JSON should be rejected")


def test_research_qualification_passes_only_with_complete_holdout_trials():
    fields = ("technology", "physics", "chemistry", "biology")
    cases = []
    trials = []
    for index in range(80):
        case_id = f"case-{index:02d}"
        split = "holdout" if index < 40 else "development"
        cases.append(_research_case(case_id, fields[index % len(fields)], split))
        if split == "holdout":
            trials.append(_trial(case_id))
    report = qualify_research_dataset(
        {"schema_version": 1, "dataset_version": "research-v1", "cases": cases},
        {"schema_version": 1, "dataset_version": "research-v1", "trials": trials},
        trials_per_case=1,
    )
    assert report["status"] == "qualified"
    assert report["metrics"]["citation_support_precision"] == 0.95
    assert all(report["gates"].values())


def test_research_qualification_rejects_critical_claim_violations():
    fields = ("technology", "physics", "chemistry", "biology")
    cases = []
    trials = []
    for index in range(80):
        case_id = f"case-{index:02d}"
        split = "holdout" if index < 40 else "development"
        cases.append(_research_case(case_id, fields[index % len(fields)], split))
        if split == "holdout":
            trial = _trial(case_id)
            if index == 0:
                trial["metrics"]["critical_unsupported_claims"] = 1
            trials.append(trial)
    report = qualify_research_dataset(
        {"schema_version": 1, "dataset_version": "research-v1", "cases": cases},
        {"schema_version": 1, "dataset_version": "research-v1", "trials": trials},
        trials_per_case=1,
    )
    assert report["status"] == "failed"
    assert not report["gates"]["critical_unsupported_or_contradicted_claims_zero"]


def test_research_qualification_requires_full_holdout_and_whole_number_counts():
    fields = ("technology", "physics", "chemistry", "biology")
    cases = []
    for index in range(40):
        case = _research_case(f"case-{index:02d}", fields[index % len(fields)], "holdout" if index < 4 else "development")
        if index == 0:
            case["gold"]["citation_total"] = 20.5
        cases.append(case)
    report = qualify_research_dataset(
        {"schema_version": 1, "dataset_version": "research-v1", "cases": cases},
        {"schema_version": 1, "dataset_version": "research-v1", "trials": []},
        trials_per_case=1,
    )

    assert report["status"] == "incomplete"
    assert any("holdout has 4 cases; minimum is 40" in error for error in report["errors"])
    assert any("citation_total must be a non-negative integer" in error for error in report["errors"])


def test_research_qualification_rejects_trial_without_completed_bundle_status():
    fields = ("technology", "physics", "chemistry", "biology")
    cases = []
    trials = []
    for index in range(80):
        case_id = f"case-{index:02d}"
        split = "holdout" if index < 40 else "development"
        cases.append(_research_case(case_id, fields[index % len(fields)], split))
        if split == "holdout":
            trials.append(_trial(case_id))
    trials[0].pop("execution_status")

    report = qualify_research_dataset(
        {"schema_version": 1, "dataset_version": "research-v1", "cases": cases},
        {"schema_version": 1, "dataset_version": "research-v1", "trials": trials},
        trials_per_case=1,
    )

    assert report["status"] == "incomplete"
    assert any("did not produce a completed research bundle" in error for error in report["errors"])


def test_research_qualification_rejects_nonpositive_trial_requirement():
    fields = ("technology", "physics", "chemistry", "biology")
    cases = []
    trials = []
    for index in range(80):
        case_id = f"case-{index:02d}"
        split = "holdout" if index < 40 else "development"
        cases.append(_research_case(case_id, fields[index % len(fields)], split))
        if split == "holdout":
            trials.append(_trial(case_id))

    report = qualify_research_dataset(
        {"schema_version": 1, "dataset_version": "research-v1", "cases": cases},
        {"schema_version": 1, "dataset_version": "research-v1", "trials": trials},
        trials_per_case=0,
    )

    assert report["status"] == "incomplete"
    assert "trials_per_case must be positive" in report["errors"]


def test_retrieval_benchmark_reports_perfect_labeled_baseline():
    dataset, results = _retrieval_benchmark()
    report = evaluate_retrieval_benchmark(dataset, results)
    assert report["status"] == "qualified"
    assert report["metrics"]["retrieval_recall_at_20"] == 1.0
    assert report["metrics"]["ranking_ndcg_at_20"] == 1.0
    assert report["metrics"]["dedup_precision"] == 1.0
    assert report["metrics"]["dedup_recall"] == 1.0


def test_retrieval_benchmark_rejects_missing_result():
    dataset, results = _retrieval_benchmark()
    results["cases"].pop()
    report = evaluate_retrieval_benchmark(dataset, results)
    assert report["status"] == "incomplete"
    assert not report["qualified"]
    assert report["errors"]


def test_retrieval_benchmark_scores_development_without_qualifying_release():
    dataset, results = _retrieval_benchmark()
    for case in dataset["cases"]:
        case["split"] = "development"
    results["dataset_fingerprint"] = hashlib.sha256(
        json.dumps(dataset, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()

    report = evaluate_retrieval_benchmark(dataset, results)

    assert report["status"] == "development_scored"
    assert report["split"] == "development"
    assert not report["qualified"]
    assert all(report["gates"].values())


def test_retrieval_benchmark_rejects_results_from_another_dataset():
    dataset, results = _retrieval_benchmark()
    results["dataset_fingerprint"] = "0" * 64

    report = evaluate_retrieval_benchmark(dataset, results)

    assert report["status"] == "incomplete"
    assert any("dataset_fingerprint" in error for error in report["errors"])


def test_retrieval_benchmark_rejects_ranked_ids_outside_corpus():
    dataset, results = _retrieval_benchmark()
    result = next(item for item in results["cases"] if item["kind"] == "retrieval")
    result["ranked_ids"] = ["not-in-the-corpus"]

    report = evaluate_retrieval_benchmark(dataset, results)

    assert report["status"] == "incomplete"
    assert any("outside the dataset corpus" in error for error in report["errors"])


def test_retrieval_benchmark_rejects_all_zero_ranking_labels():
    dataset, results = _retrieval_benchmark()
    case = next(item for item in dataset["cases"] if item["kind"] == "ranking")
    case["relevance"] = {item_id: 0 for item_id in case["relevance"]}
    results["dataset_fingerprint"] = hashlib.sha256(
        json.dumps(dataset, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()

    report = evaluate_retrieval_benchmark(dataset, results)

    assert report["status"] == "incomplete"
    assert any("at least one relevant candidate" in error for error in report["errors"])


def test_retrieval_benchmark_counts_distractors_outside_relevant_ids():
    dataset, results = _retrieval_benchmark()
    case = next(item for item in dataset["cases"] if item["kind"] == "retrieval")
    case["relevant_ids"] = [item["id"] for item in dataset["corpus"][:202]]
    results["dataset_fingerprint"] = hashlib.sha256(
        json.dumps(dataset, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()

    report = evaluate_retrieval_benchmark(dataset, results)

    assert report["status"] == "incomplete"
    assert any("200 distractors outside its relevant_ids" in error for error in report["errors"])


def test_retrieval_benchmark_verifies_cross_language_pair_metadata():
    dataset, results = _retrieval_benchmark()
    case = next(item for item in dataset["cases"] if item["kind"] == "dedup" and item["cross_language"])
    next(document for document in dataset["corpus"] if document["id"] == case["right_id"])["language"] = "en"
    results["dataset_fingerprint"] = hashlib.sha256(
        json.dumps(dataset, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()

    report = evaluate_retrieval_benchmark(dataset, results)

    assert report["status"] == "incomplete"
    assert any("marked cross-language" in error for error in report["errors"])


def test_retrieval_benchmark_reports_malformed_source_url_without_crashing():
    dataset, results = _retrieval_benchmark()
    dataset["cases"][0]["source_snapshots"][0]["url"] = "https://[invalid"
    results["dataset_fingerprint"] = hashlib.sha256(
        json.dumps(dataset, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()

    report = evaluate_retrieval_benchmark(dataset, results)

    assert report["status"] == "incomplete"
    assert any("source_snapshots[0]" in error for error in report["errors"])


def test_reviewer_calibration_measures_agreement_and_rejects_unknown_decisions():
    adjudicated = {f"case-{index:02d}": "pass" for index in range(20)}
    labels = [{"case_id": case_id, "reviewer_id": "reviewer-a", "decision": "pass"} for case_id in adjudicated]
    measured = calibrate_reviewers(adjudicated, labels)
    assert measured["status"] == "measured"
    assert measured["reviewers"]["reviewer-a"]["accuracy"] == 1.0

    labels[0]["decision"] = "maybe"
    incomplete = calibrate_reviewers(adjudicated, labels)
    assert incomplete["status"] == "incomplete"
    assert any("decision must be pass, fail, or uncertain" in error for error in incomplete["errors"])
