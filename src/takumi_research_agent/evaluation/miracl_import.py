from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

MIRACL_DATASET_URL = "https://huggingface.co/datasets/miracl/miracl"
MIRACL_ANNOTATOR_ID = "miracl-native-speaker-annotators"
MINIMUM_CASES = 40


def import_miracl_development(
    rows: Iterable[dict[str, Any]],
    *,
    source_sha256: str,
    case_limit: int = 40,
) -> dict[str, Any]:
    """Convert an exported MIRACL Indonesian dev split to Takumi's IR schema.

    The imported benchmark is development-only: it measures a general Indonesian
    text retriever, not Takumi's scientific history domain or cross-language dedup.
    """
    if len(source_sha256) != 64 or any(char not in "0123456789abcdef" for char in source_sha256):
        raise ValueError("source_sha256 must be 64 lowercase hexadecimal characters")
    if not isinstance(case_limit, int) or isinstance(case_limit, bool) or case_limit < MINIMUM_CASES:
        raise ValueError(f"case_limit must be at least {MINIMUM_CASES}")

    selected: list[dict[str, Any]] = []
    corpus_by_id: dict[str, dict[str, str]] = {}
    seen_query_ids: set[str] = set()

    for row_index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise TypeError(f"input row {row_index} must be a JSON object")
        query_id = row.get("query_id")
        query = row.get("query")
        if query_id is None or not isinstance(query, str) or not query.strip():
            raise ValueError(f"input row {row_index} needs query_id and query")
        query_id = str(query_id)
        if query_id in seen_query_ids:
            raise ValueError(f"duplicate MIRACL query_id: {query_id}")
        seen_query_ids.add(query_id)

        positives = _passages(row.get("positive_passages"), row_index=row_index, label="positive_passages")
        negatives = _passages(row.get("negative_passages"), row_index=row_index, label="negative_passages")
        if not positives:
            continue
        positive_ids = {item["id"] for item in positives}
        negative_ids = {item["id"] for item in negatives}
        if positive_ids.intersection(negative_ids):
            raise ValueError(f"input row {row_index} labels a passage both positive and negative")
        for passage in (*positives, *negatives):
            existing = corpus_by_id.get(passage["id"])
            if existing is not None and existing != passage:
                raise ValueError(f"MIRACL passage {passage['id']!r} has conflicting text")
            corpus_by_id[passage["id"]] = passage
        selected.append({
            "query_id": query_id,
            "query": query.strip(),
            "positive_ids": sorted(positive_ids),
            "negative_ids": sorted(negative_ids),
        })
        if len(selected) == case_limit:
            break

    if len(selected) < case_limit:
        raise ValueError(f"input has {len(selected)} usable queries; need at least {case_limit}")
    if len(corpus_by_id) < 201:
        raise ValueError(f"selected queries provide {len(corpus_by_id)} unique passages; need at least 201")
    if any(len(corpus_by_id) - len(item["positive_ids"]) < 200 for item in selected):
        raise ValueError("selected queries do not have at least 200 distractors outside their positive passages")

    source_snapshot = {"url": MIRACL_DATASET_URL, "sha256": source_sha256}
    corpus = [corpus_by_id[item_id] for item_id in sorted(corpus_by_id)]
    cases: list[dict[str, Any]] = []
    for item in selected:
        common = {
            "split": "development",
            "field": "general",
            "adjudicated": True,
            "reviewers": [MIRACL_ANNOTATOR_ID],
            "source_snapshots": [source_snapshot],
        }
        cases.append({
            "case_id": f"miracl-id-dev-{item['query_id']}-retrieval",
            "kind": "retrieval",
            **common,
            "query": item["query"],
            "relevant_ids": item["positive_ids"],
            "corpus_size": len(corpus),
        })
        relevance = {doc_id: 1 for doc_id in item["positive_ids"]}
        relevance.update({doc_id: 0 for doc_id in item["negative_ids"]})
        cases.append({
            "case_id": f"miracl-id-dev-{item['query_id']}-ranking",
            "kind": "ranking",
            **common,
            "query": item["query"],
            "relevance": relevance,
        })

    return {
        "schema_version": 1,
        "dataset_version": f"miracl-id-dev-v1-{source_sha256[:12]}",
        "description": "Development-only Indonesian retrieval/ranking set derived from human-annotated MIRACL dev queries. It is not a Takumi research-quality holdout and contains no deduplication labels.",
        "provenance": {
            "dataset": "MIRACL",
            "subset": "Indonesian dev split",
            "source_url": MIRACL_DATASET_URL,
            "source_sha256": source_sha256,
            "license": "Apache-2.0 per the MIRACL dataset card; verify underlying corpus terms before redistribution.",
            "annotation_note": "MIRACL reports relevance judgments made by native-speaker annotators. This imported split is for development and model comparison only.",
        },
        "corpus": corpus,
        "cases": cases,
    }


def import_miracl_jsonl(path: str | Path, *, case_limit: int = 40) -> dict[str, Any]:
    source_path = Path(path)
    source_bytes = source_path.read_bytes()
    source_sha256 = hashlib.sha256(source_bytes).hexdigest()
    rows = []
    for line_number, line in enumerate(source_bytes.decode("utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid JSON on line {line_number}: {exc.msg}") from exc
        if not isinstance(row, dict):
            raise TypeError(f"line {line_number} must contain a JSON object")
        rows.append(row)
    return import_miracl_development(rows, source_sha256=source_sha256, case_limit=case_limit)


def _passages(value: Any, *, row_index: int, label: str) -> list[dict[str, str]]:
    if not isinstance(value, list):
        raise TypeError(f"input row {row_index}.{label} must be an array")
    normalized = []
    seen: set[str] = set()
    for passage_index, passage in enumerate(value):
        if not isinstance(passage, dict):
            raise TypeError(f"input row {row_index}.{label}[{passage_index}] must be an object")
        doc_id = passage.get("docid")
        title = passage.get("title")
        text = passage.get("text")
        if doc_id is None or not isinstance(title, str) or not isinstance(text, str) or not (title.strip() or text.strip()):
            raise ValueError(f"input row {row_index}.{label}[{passage_index}] needs docid, title, and text")
        doc_id = str(doc_id)
        if doc_id in seen:
            raise ValueError(f"input row {row_index}.{label} has duplicate passage ID {doc_id!r}")
        seen.add(doc_id)
        normalized.append({"id": doc_id, "title": title.strip(), "content": text.strip(), "language": "id"})
    return normalized
