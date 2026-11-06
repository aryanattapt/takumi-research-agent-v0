from __future__ import annotations

import hashlib
import re
from pathlib import Path

from takumi_research_agent.domain.models import EvidenceSpan

from .artifacts import ArtifactStore


def _normalize_quote(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip().casefold()


class EvidenceRegistrar:
    def __init__(self, artifact_store: ArtifactStore):
        self.artifact_store = artifact_store

    def register(self, *, source_id: str, source_text: str, quote: str, locator: str | None = None) -> EvidenceSpan:
        normalized_quote = _normalize_quote(quote)
        if len(normalized_quote) < 20:
            raise ValueError("Evidence quote must contain at least 20 normalized characters")
        if normalized_quote not in _normalize_quote(source_text):
            raise ValueError("Evidence quote was not found in the captured source text")
        source_hash, _ = self.artifact_store.put("source", source_text)
        quote_hash = hashlib.sha256(normalized_quote.encode("utf-8")).hexdigest()
        evidence_id = hashlib.sha256(f"{source_id}\0{source_hash}\0{quote_hash}".encode()).hexdigest()
        return EvidenceSpan(
            evidence_id=evidence_id,
            source_id=source_id,
            quote=quote,
            locator=locator,
            content_hash=source_hash,
        )

    def verify_artifact(self, evidence: EvidenceSpan) -> bool:
        data = self.artifact_store.read(evidence.content_hash, kind="source")
        return _normalize_quote(evidence.quote) in _normalize_quote(data.decode("utf-8"))
