from __future__ import annotations

import hashlib
import json
from typing import Any

from takumi_research_agent.research.agent import history_candidates

from .research_runner import current_code_revision


def _document_text(document: dict[str, Any]) -> str:
    return " ".join(
        str(document.get(key, ""))
        for key in ("title", "summary", "content")
        if document.get(key) is not None
    ).strip()


def run_retrieval_dataset(
    dataset: dict[str, Any],
    *,
    duplicate_threshold: float = 0.10,
    code_revision: str | None = None,
) -> dict[str, Any]:
    """Run the existing lexical history retriever against a versioned benchmark.

    This is a measurable baseline, not a semantic embedding index. Freeze the
    threshold on development data before reporting holdout qualification.
    """
    if dataset.get("schema_version") != 1:
        raise ValueError("dataset schema_version must be 1")
    version = dataset.get("dataset_version")
    if not isinstance(version, str) or not version.strip():
        raise ValueError("dataset_version is required")
    if not 0 <= duplicate_threshold <= 1:
        raise ValueError("duplicate_threshold must be between 0 and 1")

    documents = dataset.get("corpus")
    if not isinstance(documents, list) or not documents:
        raise ValueError("dataset corpus must be a non-empty array")
    corpus: list[dict[str, Any]] = []
    by_id: dict[str, dict[str, Any]] = {}
    for index, document in enumerate(documents):
        if not isinstance(document, dict):
            raise TypeError(f"corpus[{index}] must be an object")
        item_id = document.get("id")
        if not isinstance(item_id, str) or not item_id.strip():
            raise ValueError(f"corpus[{index}].id is required")
        if item_id in by_id:
            raise ValueError(f"duplicate corpus id: {item_id}")
        normalized = {
            "id": item_id,
            "title": str(document.get("title", "")),
            "summary": str(document.get("summary", "")),
            "content": str(document.get("content", "")),
            "language": str(document.get("language", "")),
        }
        if not _document_text(normalized):
            raise ValueError(f"corpus[{index}] needs title, summary, or content")
        corpus.append(normalized)
        by_id[item_id] = normalized

    cases = dataset.get("cases")
    if not isinstance(cases, list):
        raise TypeError("dataset cases must be an array")
    outputs = []
    seen: set[tuple[str, str]] = set()
    for index, case in enumerate(cases):
        if not isinstance(case, dict):
            raise TypeError(f"cases[{index}] must be an object")
        case_id, kind = case.get("case_id"), case.get("kind")
        if not isinstance(case_id, str) or not case_id.strip():
            raise ValueError(f"cases[{index}].case_id is required")
        if kind not in {"retrieval", "ranking", "dedup"}:
            raise ValueError(f"cases[{index}].kind is unsupported")
        key = (kind, case_id)
        if key in seen:
            raise ValueError(f"duplicate {kind} case_id: {case_id}")
        seen.add(key)

        if kind in {"retrieval", "ranking"}:
            query = case.get("query")
            if not isinstance(query, str) or not query.strip():
                raise ValueError(f"{kind} case {case_id!r} needs a query")
            corpus_size = case.get("corpus_size", len(corpus))
            if not isinstance(corpus_size, int) or corpus_size != len(corpus):
                raise ValueError(f"{kind} case {case_id!r} corpus_size must match the provided corpus")
            ranked = history_candidates(query, corpus, limit=20)
            outputs.append({
                "kind": kind,
                "case_id": case_id,
                "ranked_ids": [item["id"] for item in ranked],
            })
            continue

        left_id, right_id = case.get("left_id"), case.get("right_id")
        if (
            not isinstance(left_id, str)
            or not isinstance(right_id, str)
            or left_id not in by_id
            or right_id not in by_id
            or left_id == right_id
        ):
            raise ValueError(f"dedup case {case_id!r} needs distinct left_id/right_id in corpus")
        left = by_id[left_id]
        right = by_id[right_id]
        query = _document_text(left)
        matches = history_candidates(query, [right], limit=1)
        score = matches[0]["similarity_hint"] if matches else 0.0
        outputs.append({
            "kind": "dedup",
            "case_id": case_id,
            "predicted_duplicate": score >= duplicate_threshold,
            "similarity_hint": score,
        })

    corpus_digest = hashlib.sha256(
        json.dumps(corpus, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return {
        "schema_version": 1,
        "dataset_version": version,
        "dataset_fingerprint": hashlib.sha256(
            json.dumps(dataset, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        ).hexdigest(),
        "metadata": {
            "retriever_version": "lexical-jaccard-v1",
            "index_version": f"{version}:{corpus_digest}",
            "code_revision": code_revision or current_code_revision(),
            "duplicate_threshold": duplicate_threshold,
        },
        "cases": outputs,
        "limitations": [
            "This run measures the current lexical Jaccard retriever with a small bilingual term map; it is not semantic retrieval.",
            "Tune duplicate_threshold on development cases and freeze it before scoring holdout cases.",
        ],
    }
