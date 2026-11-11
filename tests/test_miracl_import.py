import hashlib
import json

import pytest

from takumi_research_agent.evaluation.miracl_import import (
    import_miracl_development,
    import_miracl_jsonl,
)
from takumi_research_agent.evaluation.retrieval import evaluate_retrieval_benchmark
from takumi_research_agent.evaluation.retrieval_runner import run_retrieval_dataset


def _miracl_rows(count: int = 40) -> list[dict]:
    rows = []
    for index in range(count):
        rows.append({
            "query_id": str(index),
            "query": f"Topik sains Indonesia nomor {index}",
            "positive_passages": [{
                "docid": f"positive-{index}",
                "title": f"Dokumen relevan {index}",
                "text": f"Informasi yang menjawab topik sains {index}",
            }],
            "negative_passages": [
                {
                    "docid": f"negative-{index}-{negative}",
                    "title": f"Dokumen tidak relevan {index}-{negative}",
                    "text": f"Informasi yang berbeda {index}-{negative}",
                }
                for negative in range(5)
            ],
        })
    return rows


def test_miracl_dev_import_runs_through_retrieval_evaluator():
    dataset = import_miracl_development(_miracl_rows(), source_sha256="a" * 64)
    results = run_retrieval_dataset(dataset, code_revision="test-revision")

    report = evaluate_retrieval_benchmark(dataset, results)

    assert len(dataset["corpus"]) == 240
    assert report["status"] == "development_scored"
    assert not report["qualified"]
    assert report["split"] == "development"
    assert report["case_counts"] == {"retrieval": 40, "ranking": 40, "dedup": 0, "cross_language_dedup": 0}
    assert report["metrics"]["retrieval_queries_scored"] == 40
    assert report["metrics"]["ranking_queries_scored"] == 40


def test_miracl_jsonl_import_records_hash_of_local_export(tmp_path):
    source_path = tmp_path / "miracl-id-dev.jsonl"
    source = "\n".join(json.dumps(row, ensure_ascii=False) for row in _miracl_rows()) + "\n"
    source_path.write_text(source, encoding="utf-8")

    dataset = import_miracl_jsonl(source_path)

    expected_sha256 = hashlib.sha256(source.encode("utf-8")).hexdigest()
    assert dataset["provenance"]["source_sha256"] == expected_sha256
    assert dataset["dataset_version"] == f"miracl-id-dev-v1-{expected_sha256[:12]}"


def test_miracl_import_requires_enough_passages_and_valid_source_hash():
    rows = _miracl_rows()
    with pytest.raises(ValueError, match="source_sha256 must be 64 lowercase hexadecimal characters"):
        import_miracl_development(rows, source_sha256="not-a-hash")

    small_rows = _miracl_rows()
    for row in small_rows:
        row["negative_passages"] = row["negative_passages"][:1]
        for passage in row["negative_passages"]:
            passage.update({"docid": "shared-negative", "title": "Satu artikel distractor", "text": "Konten distractor"})
    with pytest.raises(ValueError, match="need at least 201"):
        import_miracl_development(small_rows, source_sha256="b" * 64)
