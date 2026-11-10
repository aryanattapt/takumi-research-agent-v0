import asyncio

from takumi_research_agent.config import Settings
from takumi_research_agent.evaluation import research_runner
from takumi_research_agent.evaluation.qualification import _validate_trial
from takumi_research_agent.evaluation.research_runner import (
    build_research_trial_document,
    create_research_review_packet,
    validate_research_run_dataset,
)
from takumi_research_agent.evaluation.retrieval_runner import run_retrieval_dataset
from takumi_research_agent.evaluation.semantic_retrieval import (
    run_semantic_retrieval_dataset,
)


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


def test_semantic_retrieval_runner_ranks_cross_language_candidates_and_scores_dedup():
    class FakeEmbeddings:
        async def create(self, *, model, input, encoding_format):
            from types import SimpleNamespace

            rows = []
            for index, text in enumerate(input):
                normalized = text.casefold()
                vector = [1.0, 0.0] if any(term in normalized for term in ("quantum", "qubit", "kuantum")) else [0.0, 1.0]
                rows.append(SimpleNamespace(index=index, embedding=vector))
            return SimpleNamespace(data=rows)

    class FakeClient:
        embeddings = FakeEmbeddings()

    dataset = {
        "schema_version": 1,
        "dataset_version": "retrieval-v2",
        "corpus": [
            {"id": "en", "title": "Quantum computing and qubits", "language": "en"},
            {"id": "id", "title": "Komputasi kuantum dan qubit", "language": "id"},
            {"id": "noise", "title": "Bread and baking", "language": "en"},
        ],
        "cases": [
            {"case_id": "cross-language", "kind": "retrieval", "query": "Komputasi kuantum", "corpus_size": 3},
            {"case_id": "duplicate-pair", "kind": "dedup", "left_id": "en", "right_id": "id"},
        ],
    }

    report = asyncio.run(
        run_semantic_retrieval_dataset(
            dataset,
            api_key="test-key",
            model="multilingual-test-model",
            code_revision="revision-test",
            batch_size=2,
            client=FakeClient(),
        )
    )

    assert report["metadata"]["retriever_version"] == "openai-compatible-cosine-v1"
    assert report["metadata"]["embedding_dimensions"] == 2
    assert report["cases"][0]["ranked_ids"][:2] == ["en", "id"]
    assert report["cases"][1]["predicted_duplicate"] is True
    assert "not wired into production" in report["limitations"][0]


def test_research_runner_concurrency_is_bounded_and_recorded(tmp_path, monkeypatch):
    active = 0
    maximum_active = 0

    class FakeProvider:
        async def close(self):
            return None

    async def fake_run_case(case, **kwargs):
        nonlocal active, maximum_active
        active += 1
        maximum_active = max(maximum_active, active)
        await asyncio.sleep(0.01)
        active -= 1
        return {
            "status": "completed",
            "reason": "agent_finished",
            "duration_seconds": 0.01,
            "physical_requests": 1,
            "tool_actions": 1,
            "tokens_input": 10,
            "tokens_output": 10,
            "research": {"bundle": {}, "sources": [], "evidence": []},
            "trace": [],
        }

    monkeypatch.setattr(research_runner, "_make_provider", lambda *args, **kwargs: FakeProvider())
    monkeypatch.setattr(research_runner, "_run_one_case", fake_run_case)
    dataset = {"schema_version": 1, "dataset_version": "research-v1", "cases": [_dataset_case()]}

    report = asyncio.run(
        research_runner.run_research_trials(
            dataset,
            output_path=tmp_path / "runs.json",
            split="holdout",
            trials_per_case=4,
            provider_name="openai-compatible",
            model="test-model",
            api_key="test-key",
            base_url=None,
            timeout_seconds=10,
            prompt_version="prompt-v1",
            tool_schema_version="tools-v1",
            policy_version="policy-v1",
            adapter_version="adapter-v1",
            code_revision="revision-test",
            concurrency=2,
            settings=Settings.from_env(env_file=None),
        )
    )

    assert maximum_active == 2
    assert report["metadata"]["run_concurrency"] == "2"
    assert len(report["runs"]) == 4
