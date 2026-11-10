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
    critical_claims_supported = all(
        claim.support == "supported"
        for claim in pair.claims
        if claim.importance == "critical"
    )
    important_claims = [claim for claim in pair.claims if claim.importance in {"critical", "material"}]
    unsupported_important_claims = [
        claim for claim in important_claims if claim.support in {"contradicted", "unknown"}
    ]
    unsupported_critical_claims = [
        claim for claim in pair.claims if claim.importance == "critical" and claim.support != "supported"
    ]
    partially_supported_material_claims = [
        claim for claim in pair.claims if claim.importance == "material" and claim.support == "partial"
    ]
    unresolved_context_claims = [
        claim for claim in pair.claims if claim.importance == "context" and claim.support != "supported"
    ]
    contradicted_claims = [claim for claim in pair.claims if claim.support == "contradicted"]
    has_retracted_source = any(source.retracted for source in pair.sources)
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
    if unsupported_critical_claims:
        reasons.append("A critical claim is unsupported, contradicted, or unresolved.")
    if unsupported_important_claims:
        reasons.append("A material or critical claim is contradicted or lacks confirmed support.")
    if partially_supported_material_claims:
        reasons.append("A material claim is only partially supported; human revision is required before publication.")
    if unresolved_context_claims:
        reasons.append("A context claim is not fully supported; human review is required before publication.")
    if has_retracted_source:
        reasons.append("A retracted source is present and must be removed or resolved.")

    if reasons:
        must_block = (
            not credible_sources
            or not review.material_claims_supported
            or bool(unsupported_important_claims)
            or bool(unsupported_critical_claims)
            or bool(contradicted_claims)
            or has_retracted_source
        )
        return EditorialDecision(
            decision="blocked" if must_block else "draft",
            reasons=reasons,
            journal_support_sufficient=False,
            critical_claims_supported=critical_claims_supported,
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
        critical_claims_supported=critical_claims_supported,
        both_languages_complete=complete,
    )
