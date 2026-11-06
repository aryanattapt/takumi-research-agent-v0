from __future__ import annotations

from .models import ArticlePair, EditorialDecision, ReviewResult


def editorial_decision(
    pair: ArticlePair,
    *,
    review: ReviewResult,
    novelty_clear: bool,
    credible_sources: bool,
) -> EditorialDecision:
    """Fail closed while keeping draft content subject to factual quality checks."""
    reasons: list[str] = []
    required_languages = {"id", "en"}
    complete = set(pair.articles) == required_languages
    critical_supported = all(
        claim.support == "supported"
        for claim in pair.claims
        if claim.importance == "critical"
    )
    if not complete:
        reasons.append("Both Indonesian and English versions are required.")
    if not novelty_clear:
        reasons.append("Topic novelty is unresolved or the blog history is incomplete.")
    if not credible_sources:
        reasons.append("No sufficiently credible primary source supports the topic.")
    if not review.material_claims_supported:
        reasons.append("At least one material claim lacks adequate evidence.")
    if not review.bilingual_parity:
        reasons.append("The Indonesian and English versions are materially inconsistent.")
    if not review.approved or review.reader_score < 4:
        reasons.append("Editorial review did not meet the reader-quality gate.")
    if any(claim.importance == "critical" and claim.support != "supported" for claim in pair.claims):
        reasons.append("A critical claim is unsupported, contradicted, or unresolved.")
    if any(source.retracted for source in pair.sources):
        reasons.append("A retracted source is present and must be removed or resolved.")

    if reasons:
        return EditorialDecision(
            decision="blocked" if not credible_sources or not review.material_claims_supported or not critical_supported else "draft",
            reasons=reasons,
            journal_support_sufficient=False,
            critical_claims_supported=critical_supported,
            both_languages_complete=complete,
        )

    journal_support = any(
        source.source_type == "journal"
        and source.peer_reviewed is True
        and source.peer_review_basis in {"article_record", "journal_policy"}
        and source.relevant_to_topic
        and source.read_status in {"abstract", "full_text"}
        for source in pair.sources
    )
    return EditorialDecision(
        decision="publish" if journal_support else "draft",
        reasons=[] if journal_support else ["Credible evidence is available, but relevant peer-reviewed journal support is not confirmed."],
        journal_support_sufficient=journal_support,
        critical_claims_supported=critical_supported,
        both_languages_complete=complete,
    )
