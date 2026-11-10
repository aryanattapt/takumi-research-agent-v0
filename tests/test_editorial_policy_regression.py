from __future__ import annotations

import json
from pathlib import Path

import pytest

from takumi_research_agent.domain.models import ArticlePair, ClaimEvidence, LocalizedArticle, ReviewResult, SourceRecord
from takumi_research_agent.domain.policy import editorial_decision


FIXTURE = Path(__file__).parent / "fixtures" / "editorial-policy-regression-v1.json"
POLICY_CASES = json.loads(FIXTURE.read_text(encoding="utf-8"))["cases"]


def _base_pair(case: dict[str, object]) -> ArticlePair:
    source = SourceRecord(
        source_id="journal-source-001",
        title="A relevant peer-reviewed study",
        url="https://journal.example/research",
        source_type="journal",
        peer_reviewed=True,
        peer_review_basis="journal_policy",
        relevant_to_topic=True,
        read_status="full_text",
        retracted=bool(case.get("retracted", False)),
    )
    claim = ClaimEvidence(
        claim="The study reports a measured outcome under defined conditions.",
        evidence_ids=["evidence-00001"],
        importance=str(case["importance"]),
        support=str(case["support"]),
    )
    articles = {
        "id": LocalizedArticle(
            title="Penjelasan hasil penelitian",
            summary="Ringkasan hasil penelitian dengan konteks, batasan, dan bahasa yang mudah dipahami.",
            body_markdown="Penjelasan hasil penelitian untuk pembaca umum. " * 10,
        ),
        "en": LocalizedArticle(
            title="Explaining a research finding",
            summary="A clear summary of the finding, its context, and the limits of the available evidence.",
            body_markdown="An accessible explanation of the research finding. " * 10,
        ),
    }
    if case.get("both_languages") is False:
        articles.pop("en")
    if case.get("journal_support") is False:
        source = source.model_copy(update={"peer_reviewed": None, "peer_review_basis": "unknown"})
    return ArticlePair(
        pair_id="policy-regression-0001",
        topic="A measured research outcome and its interpretation",
        field="biology",
        research_question="What does the reported measured outcome mean?",
        articles=articles,
        sources=[source],
        claims=[claim],
        novelty_reason="This question is not covered by the current verified blog history.",
    )


@pytest.mark.parametrize("case", POLICY_CASES, ids=[case["id"] for case in POLICY_CASES])
def test_versioned_policy_regression_cases(case: dict[str, object]):
    review = ReviewResult(
        approved=bool(case.get("review_approved", True)),
        reader_score=int(case.get("reader_score", 5)),
        material_claims_supported=bool(case.get("material_claims_supported", True)),
        bilingual_parity=bool(case.get("bilingual_parity", True)),
    )
    decision = editorial_decision(
        _base_pair(case),
        review=review,
        novelty_clear=bool(case.get("novelty_clear", True)),
        credible_sources=bool(case.get("credible_sources", True)),
    )
    assert decision.decision == case["expected"]
