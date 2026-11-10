from __future__ import annotations

import math
from typing import Any

MAX_EMBEDDING_BATCH_SIZE = 128


def normalize_embedding(value: Any, *, name: str) -> list[float]:
    if not isinstance(value, (list, tuple)) or not value:
        raise ValueError(f"embedding for {name} must be a non-empty vector")
    try:
        vector = [float(component) for component in value]
    except (TypeError, ValueError) as exc:
        raise ValueError(f"embedding for {name} contains a non-numeric value") from exc
    if any(not math.isfinite(component) for component in vector):
        raise ValueError(f"embedding for {name} contains a non-finite value")
    norm = math.sqrt(sum(component * component for component in vector))
    if norm == 0:
        raise ValueError(f"embedding for {name} has zero magnitude")
    return [component / norm for component in vector]


async def embed_texts(client, texts: list[str], *, model: str, batch_size: int = 64) -> list[list[float]]:
    if not 1 <= batch_size <= MAX_EMBEDDING_BATCH_SIZE:
        raise ValueError(f"batch_size must be between 1 and {MAX_EMBEDDING_BATCH_SIZE}")
    embeddings: list[list[float]] = []
    dimensions: int | None = None
    for start in range(0, len(texts), batch_size):
        batch = texts[start : start + batch_size]
        response = await client.embeddings.create(model=model, input=batch, encoding_format="float")
        rows = sorted(response.data, key=lambda item: item.index)
        if len(rows) != len(batch) or [row.index for row in rows] != list(range(len(batch))):
            raise ValueError("embedding response is missing or duplicating input indexes")
        for offset, row in enumerate(rows):
            vector = normalize_embedding(row.embedding, name=f"text[{start + offset}]")
            if dimensions is None:
                dimensions = len(vector)
            elif len(vector) != dimensions:
                raise ValueError("embedding response dimensions changed within one run")
            embeddings.append(vector)
    return embeddings
