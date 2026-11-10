from takumi_research_agent.evaluation.qualification import _validate_trial
from takumi_research_agent.evaluation.research_runner import (
    build_research_trial_document,
    create_research_review_packet,
    validate_research_run_dataset,
)
from takumi_research_agent.evaluation.retrieval_runner import run_retrieval_dataset


def _dataset_case():
    return {
        "case_id": "science-001",
        "split": "holdout",
        "field": "biology",
        "topic": "How mRNA vaccines train immune cells",
        "history_complete": True,
        "blog_history": [],
        "trend_metrics": {"source_count": 2},
        "adjudication": {"adjudicated": True, "reviewers": ["reviewer-a"], "decision": "pass"},
        "gold": {},
    }


def _run_document():
    metadata = {
        "provider": "openai-compatible",
        "model": "model-test",
        "prompt_version": "research-v1",
        "tool_schema_version": "tools-v1",
        "policy_version": "policy-v1",
        "adapter_version": "adapter-v1",
        "code_revision": "revision-test",
        "dataset_version": "research-v1",
    }
    return {
        "schema_version": 1,
        "dataset_version": "research-v1",
        "metadata": metadata,
        "runs": [{
            "run_id": "run-1",
            "case_id": "science-001",
            "trial_index": 1,
            "execution": {
                "status": "completed",
                "reason": "agent_finished",
                "research": {
                    "bundle": {"topic": "How mRNA vaccines train immune cells"},
                    "sources": [{"title": "A source"}],
                    "evidence": [{"quote": "A source quote"}],
                },
            },
        }],
    }


def test_run_dataset_requires_explicit_complete_history_snapshot():
    dataset = {"schema_version": 1, "dataset_version": "research-v1", "cases": [_dataset_case()]}
    assert validate_research_run_dataset(dataset, split="holdout") == dataset["cases"]

    dataset["cases"][0].pop("history_complete")
    try:
        validate_research_run_dataset(dataset, split="holdout")
    except ValueError as exc:
        assert "history_complete must be an explicit boolean" in str(exc)
    else:
        raise AssertionError("missing history completeness must be rejected")


def test_review_packet_omits_gold_and_provider_identity_and_builds_qualification_rows():
    dataset = {"schema_version": 1, "dataset_version": "research-v1", "cases": [_dataset_case()]}
    runs = _run_document()
    packet = create_research_review_packet(dataset, runs)
    assert "gold" not in packet["reviews"][0]
    assert "metadata" not in packet["reviews"][0]
    assert len(packet["reviews"]) == 1

    review = packet["reviews"][0]["review"]
    review["adjudicated"] = True
    review["reviewers"] = ["independent-reviewer"]
    review["metrics"] = {
        "critical_unsupported_claims": 0,
        "contradicted_critical_claims": 0,
        "fabricated_sources": 0,
        "fabricated_quotes": 0,
        "journal_policy_bypasses": 0,
        "supported_citations": 1,
        "citation_total": 1,
        "numeric_date_unit_parity_failures": 0,
        "numeric_date_unit_parity_checks": 1,
        "reader_score": 4,
    }
    trials = build_research_trial_document(dataset, runs, packet)
    assert trials["trials"][0]["metadata"] == runs["metadata"]
    assert trials["trials"][0]["evaluation"]["adjudicated"] is True
    assert trials["trials"][0]["metrics"]["reader_score"] == 4


def test_lexical_retrieval_runner_records_version_and_ranked_candidates():
    corpus = [
        {"id": "relevant", "title": "Komputasi kuantum dan qubit", "language": "id"},
        *[
            {"id": f"noise-{index}", "title": f"Food recipe number {index}", "language": "en"}
            for index in range(200)
        ],
    ]
    dataset = {
        "schema_version": 1,
        "dataset_version": "retrieval-v1",
        "corpus": corpus,
        "cases": [{
            "case_id": "retrieval-001",
            "kind": "retrieval",
            "query": "quantum computing qubits",
            "corpus_size": 201,
        }],
    }

    results = run_retrieval_dataset(dataset, code_revision="revision-test")

    assert results["metadata"]["retriever_version"] == "lexical-jaccard-v1"
    assert results["metadata"]["code_revision"] == "revision-test"
    assert results["cases"][0]["ranked_ids"][0] == "relevant"


def test_qualification_rejects_noncompleted_research_runner_outputs():
    errors = _validate_trial(
        {"case_id": "science-001", "execution_status": "budget_exhausted"},
        case_ids={"science-001"},
        dataset_version="research-v1",
    )
    assert any("did not produce a completed research bundle" in error for error in errors)
