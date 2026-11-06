from __future__ import annotations

import hashlib

import pytest

from takumi_research_agent.domain.models import ArticlePair, ClaimEvidence, EvidenceSpan, LocalizedArticle, ReviewResult, SourceRecord
from takumi_research_agent.domain.policy import editorial_decision
from takumi_research_agent.editorial.content import render_final_body
from takumi_research_agent.research.artifacts import ArtifactIntegrityError, ArtifactStore
from takumi_research_agent.research.evidence import EvidenceRegistrar


def test_publish_requires_relevant_journal_support_and_bilingual_quality():
    source = SourceRecord(source_id="source-0001", title="Peer reviewed", url="https://journal.example/article", source_type="journal", peer_reviewed=True, peer_review_basis="journal_policy", relevant_to_topic=True, read_status="full_text")
    pair = ArticlePair(
        pair_id="pair-00000001", topic="A specific verified science topic", field="biology", research_question="What does this finding mean?",
        articles={"id": LocalizedArticle(title="Judul artikel sains", summary="Ringkasan untuk pembaca awam yang menjelaskan konteks temuan.", body_markdown="Tulisan ilmiah untuk pembaca awam " * 15), "en": LocalizedArticle(title="A clear science article", summary="A reader friendly explanation that explains the central finding and its limits.", body_markdown="An accessible scientific explanation " * 15)},
        sources=[source], claims=[ClaimEvidence(claim="The reported result changed the measured outcome.", evidence_ids=["evidence-00001"], importance="critical", support="supported")], novelty_reason="This question is not covered in the verified blog history.",
    )
    review = ReviewResult(approved=True, reader_score=4, material_claims_supported=True, bilingual_parity=True)
    decision = editorial_decision(pair, review=review, novelty_clear=True, credible_sources=True)
    assert decision.decision == "publish"
    no_journal = pair.model_copy(update={"sources": [source.model_copy(update={"peer_reviewed": None})]})
    assert editorial_decision(no_journal, review=review, novelty_clear=True, credible_sources=True).decision == "draft"


def test_unsupported_material_claim_blocks_publication():
    source = SourceRecord(source_id="source-0001", title="Primary source", url="https://example.org/source", source_type="official", read_status="full_text")
    pair = ArticlePair(
        pair_id="pair-00000002", topic="A specific verified science topic", field="physics", research_question="What does the result imply?",
        articles={"id": LocalizedArticle(title="Judul artikel fisika", summary="Ringkasan ringkas untuk pembaca umum tentang topik fisika.", body_markdown="Isi tulisan sains " * 30), "en": LocalizedArticle(title="An accessible physics story", summary="A short lay explanation of what this physics result says and cannot say.", body_markdown="Accessible scientific writing " * 30)},
        sources=[source], claims=[ClaimEvidence(claim="A material assertion without adequate support.", evidence_ids=["evidence-00002"], importance="critical", support="unknown")], novelty_reason="The blog history has no close match for the research question.",
    )
    review = ReviewResult(approved=True, reader_score=5, material_claims_supported=True, bilingual_parity=True)
    assert editorial_decision(pair, review=review, novelty_clear=True, credible_sources=True).decision == "blocked"


def test_content_renderer_blocks_scripts_and_dangerous_markdown_links():
    body = "# Heading\n\nA useful paragraph. <script>alert(1)</script>"
    html = render_final_body(body, output_format="html")
    assert "<script" not in html.lower()
    with pytest.raises(ValueError, match="protocol"):
        render_final_body("[click](javascript:alert(1))", output_format="markdown")
    with pytest.raises(ValueError, match="images"):
        render_final_body("![pixel](https://tracking.example/p.png)", output_format="markdown")


def test_evidence_must_match_and_artifact_hash_is_checked(tmp_path):
    store = ArtifactStore(tmp_path / "artifacts")
    registrar = EvidenceRegistrar(store)
    source = "A study found that the measured samples changed after the treatment under controlled conditions."
    quote = "the measured samples changed after the treatment under controlled conditions"
    evidence = registrar.register(source_id="doi:10.1/example", source_text=source, quote=quote)
    assert registrar.verify_artifact(evidence)
    with pytest.raises(ValueError, match="not found"):
        registrar.register(source_id="doi:10.1/example", source_text=source, quote="An invented result that does not appear here at all")
    _, path = store.put("source", source)
    path.write_text("tampered", encoding="utf-8")
    with pytest.raises(ArtifactIntegrityError):
        store.read(hashlib.sha256(source.encode()).hexdigest(), kind="source")
