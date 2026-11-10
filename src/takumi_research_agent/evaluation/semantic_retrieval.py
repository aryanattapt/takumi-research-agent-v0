from __future__ import annotations

import hashlib
import json
from typing import Any

from openai import AsyncOpenAI

from takumi_research_agent.providers.embeddings import embed_texts

from .research_runner import current_code_revision

MAX_EMBEDDING_TEXT_CHARS = 8_000
MAX_EMBEDDING_BATCH_SIZE = 128


def _text(document: dict[str, Any]) -> str:
    value = " ".join(
        str(document.get(field, ""))
        for field in ("title", "summary", "content")
        if document.get(field) is not None
    ).strip()
    return value[:MAX_EMBEDDING_TEXT_CHARS]


def _cosine(left: list[float], right: list[float]) -> float:
    if len(left) != len(right):
        raise ValueError("embedding vector dimensions do not match")
    return sum(a * b for a, b in zip(left, right, strict=True))


async def run_semantic_retrieval_dataset(
    dataset: dict[str, Any],
    *,
    api_key: str,
    model: str,
    base_url: str | None = None,
    timeout_seconds: float = 90,
    batch_size: int = 64,
    duplicate_threshold: float = 0.80,
    code_revision: str | None = None,
    client=None,
) -> dict[str, Any]:
    """Build an in-memory embedding index and generate retrieval/ranking/dedup predictions.

    Uses an OpenAI-compatible embeddings endpoint. The chosen model should support
    Indonesian-English cross-language matching. This benchmark runner does not
    alter the production history-retrieval path or persist embeddings.
    """
    if dataset.get("schema_version") != 1:
        raise ValueError("dataset schema_version must be 1")
    version = dataset.get("dataset_version")
    if not isinstance(version, str) or not version.strip():
        raise ValueError("dataset_version is required")
    if not model.strip() or not api_key:
        raise ValueError("embedding model and API key are required")
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    if not 1 <= batch_size <= MAX_EMBEDDING_BATCH_SIZE:
        raise ValueError(f"batch_size must be between 1 and {MAX_EMBEDDING_BATCH_SIZE}")
    if not isinstance(duplicate_threshold, (int, float)) or isinstance(duplicate_threshold, bool) or not -1 <= duplicate_threshold <= 1:
        raise ValueError("duplicate_threshold must be between -1 and 1")

    documents = dataset.get("corpus")
    if not isinstance(documents, list) or not documents:
        raise ValueError("dataset corpus must be a non-empty array")
    corpus: list[dict[str, str]] = []
    by_id: dict[str, dict[str, str]] = {}
    for index, document in enumerate(documents):
        if not isinstance(document, dict):
            raise TypeError(f"corpus[{index}] must be an object")
        item_id = document.get("id")
        if not isinstance(item_id, str) or not item_id.strip():
            raise ValueError(f"corpus[{index}].id is required")
        if item_id in by_id:
            raise ValueError(f"duplicate corpus id: {item_id}")
        text = _text(document)
        if not text:
            raise ValueError(f"corpus[{index}] needs title, summary, or content")
        normalized = {"id": item_id, "text": text}
        corpus.append(normalized)
        by_id[item_id] = normalized

    cases = dataset.get("cases")
    if not isinstance(cases, list):
        raise TypeError("dataset cases must be an array")
    case_queries: dict[tuple[str, str], str] = {}
    seen: set[tuple[str, str]] = set()
    query_texts: list[str] = []
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
            if case.get("corpus_size", len(corpus)) != len(corpus):
                raise ValueError(f"{kind} case {case_id!r} corpus_size must match the provided corpus")
            normalized_query = query.strip()[:MAX_EMBEDDING_TEXT_CHARS]
            case_queries[key] = normalized_query
            query_texts.append(normalized_query)
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

    embedding_inputs = [document["text"] for document in corpus] + list(dict.fromkeys(query_texts))
    owned_client = client is None
    client = client or AsyncOpenAI(
        api_key=api_key,
        base_url=base_url,
        timeout=timeout_seconds,
        max_retries=0,
    )
    try:
        vectors = await embed_texts(client, embedding_inputs, model=model, batch_size=batch_size)
    finally:
        if owned_client:
            await client.close()

    doc_vectors = {document["id"]: vectors[index] for index, document in enumerate(corpus)}
    query_vector_offset = len(corpus)
    query_vectors = {
        query: vectors[query_vector_offset + index]
        for index, query in enumerate(dict.fromkeys(query_texts))
    }
    outputs = []
    for case in cases:
        case_id, kind = case["case_id"], case["kind"]
        if kind in {"retrieval", "ranking"}:
            query_vector = query_vectors[case_queries[(kind, case_id)]]
            ranked = sorted(
                ((document["id"], _cosine(query_vector, doc_vectors[document["id"]])) for document in corpus),
                key=lambda item: (-item[1], item[0]),
            )
            outputs.append({"kind": kind, "case_id": case_id, "ranked_ids": [item_id for item_id, _ in ranked[:20]]})
        else:
            left = doc_vectors[case["left_id"]]
            right = doc_vectors[case["right_id"]]
            similarity = _cosine(left, right)
            outputs.append({
                "kind": kind,
                "case_id": case_id,
                "predicted_duplicate": similarity >= duplicate_threshold,
                "similarity_hint": round(similarity, 6),
            })

    corpus_digest = hashlib.sha256(
        json.dumps(corpus, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()
    index_digest = hashlib.sha256(f"{model}:{corpus_digest}".encode()).hexdigest()
    dataset_fingerprint = hashlib.sha256(
        json.dumps(dataset, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()
    return {
        "schema_version": 1,
        "dataset_version": version,
        "dataset_fingerprint": dataset_fingerprint,
        "metadata": {
            "retriever_version": "openai-compatible-cosine-v1",
            "embedding_model": model,
            "index_version": f"{model}:{index_digest}",
            "code_revision": code_revision or current_code_revision(),
            "duplicate_threshold": float(duplicate_threshold),
            "embedding_dimensions": len(vectors[0]),
            "batch_size": batch_size,
        },
        "cases": outputs,
        "limitations": [
            "The embedding index is built in memory for this run and is not wired into production research history retrieval.",
            "A cross-language benchmark should use a model documented to support Indonesian and English.",
            "Tune duplicate_threshold on development labels and freeze it before scoring holdout cases.",
            "Corpus text and queries are sent to the configured embeddings endpoint; use only data approved for that provider.",
        ],
    }
