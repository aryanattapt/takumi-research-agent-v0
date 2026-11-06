from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl


class DomainModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


TopicField = Literal["technology", "physics", "chemistry", "biology"]
Language = Literal["id", "en"]


class SourceRecord(DomainModel):
    source_id: str = Field(min_length=8, max_length=180)
    title: str = Field(min_length=4, max_length=500)
    url: HttpUrl
    source_type: Literal["journal", "preprint", "official", "dataset", "other"]
    publication_date: str | None = Field(default=None, max_length=40)
    venue: str | None = Field(default=None, max_length=300)
    peer_reviewed: bool | None = None
    peer_review_basis: Literal["article_record", "journal_policy", "unknown"] = "unknown"
    peer_review_evidence_urls: list[HttpUrl] = Field(default_factory=list, max_length=10)
    relevant_to_topic: bool = False
    read_status: Literal["metadata", "abstract", "full_text", "unavailable"] = "metadata"
    retracted: bool = False
    content_hash: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    limitations: list[str] = Field(default_factory=list, max_length=20)


class EvidenceSpan(DomainModel):
    evidence_id: str = Field(min_length=8, max_length=220)
    source_id: str = Field(min_length=8, max_length=180)
    quote: str = Field(min_length=20, max_length=2_000)
    locator: str | None = Field(default=None, max_length=300)
    content_hash: str = Field(pattern=r"^[a-f0-9]{64}$")


class ClaimEvidence(DomainModel):
    claim: str = Field(min_length=12, max_length=700)
    evidence_ids: list[str] = Field(min_length=1, max_length=10)
    importance: Literal["critical", "material", "context"] = "material"
    support: Literal["supported", "partial", "contradicted", "unknown"] = "unknown"
    qualification: str | None = Field(default=None, max_length=500)


class LocalizedArticle(DomainModel):
    title: str = Field(min_length=8, max_length=180)
    summary: str = Field(min_length=40, max_length=500)
    body_markdown: str = Field(min_length=300, max_length=30_000)


class ArticlePair(DomainModel):
    pair_id: str = Field(min_length=12, max_length=120)
    topic: str = Field(min_length=12, max_length=240)
    field: TopicField
    research_question: str = Field(min_length=12, max_length=500)
    articles: dict[Language, LocalizedArticle]
    sources: list[SourceRecord] = Field(min_length=1, max_length=30)
    evidence: list[EvidenceSpan] = Field(default_factory=list, max_length=100)
    claims: list[ClaimEvidence] = Field(min_length=1, max_length=40)
    novelty_reason: str = Field(min_length=20, max_length=1_000)


class EditorialDecision(DomainModel):
    decision: Literal["publish", "draft", "skip", "blocked"]
    reasons: list[str] = Field(default_factory=list, max_length=30)
    journal_support_sufficient: bool = False
    critical_claims_supported: bool = False
    both_languages_complete: bool = False


class ReviewResult(DomainModel):
    approved: bool
    reader_score: int = Field(ge=1, le=5)
    material_claims_supported: bool
    bilingual_parity: bool
    findings: list[str] = Field(default_factory=list, max_length=30)
