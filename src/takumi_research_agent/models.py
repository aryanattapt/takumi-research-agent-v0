from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class LocalizedArticle(StrictModel):
    title: str = Field(min_length=8, max_length=180)
    body: str = Field(min_length=300, max_length=20_000)


class ClaimEvidence(StrictModel):
    claim: str = Field(min_length=12, max_length=500)
    evidence_id: str = Field(min_length=4, max_length=120)
    quote: str = Field(min_length=30, max_length=2_000)


class ResearchBundle(StrictModel):
    field: Literal["technology", "physics", "chemistry", "biology"]
    topic: str = Field(min_length=12, max_length=240)
    research_question: str = Field(min_length=12, max_length=500)
    topic_note: str = Field(min_length=40, max_length=1_000)
    topic_unique: bool
    novelty_reason: str = Field(min_length=30, max_length=1_000)
    paper_doi: str = Field(min_length=5, max_length=180)
    paper_title: str = Field(min_length=8, max_length=500)
    articles: dict[Literal["id", "en"], LocalizedArticle]
    claims: list[ClaimEvidence] = Field(min_length=3, max_length=20)


class ReviewFinding(StrictModel):
    severity: Literal["material", "minor"]
    article_language: Literal["id", "en", "both"]
    issue: str = Field(min_length=12, max_length=500)
    evidence_id: str | None = None


class EditorialReview(StrictModel):
    approved: bool
    language_parity: bool
    findings: list[ReviewFinding] = Field(default_factory=list, max_length=20)
