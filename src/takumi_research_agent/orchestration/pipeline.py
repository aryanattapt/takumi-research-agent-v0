from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import select

from takumi_research_agent.agent.runtime import RequestBudget
from takumi_research_agent.blogs.adapter import BlogApiAdapter, BlogCapabilities
from takumi_research_agent.blogs.contracts import BlogMapping, Operation
from takumi_research_agent.config import Settings
from takumi_research_agent.delivery.service import DeliveryBlocked, DeliveryService
from takumi_research_agent.discovery.trends import TrendCandidate, TrendCollector
from takumi_research_agent.domain.policy import editorial_decision
from takumi_research_agent.editorial.service import EditorialService
from takumi_research_agent.persistence.models import (
    ArticlePairRecord,
    ArticleVersion,
    BlogHistory,
    BlogProfile,
    ContentLedger,
    EvidenceSpanRecord,
    GlobalSettings,
    ProviderProfile,
    Run,
    RunEvent,
    SourceRecord as SourceRecordRow,
    TrendObservation,
)
from takumi_research_agent.providers.anthropic_native import AnthropicProvider
from takumi_research_agent.providers.base import ProviderConfig
from takumi_research_agent.providers.openai_compatible import OpenAICompatibleProvider
from takumi_research_agent.research.agent import ResearchToolbox, history_candidates
from takumi_research_agent.research.artifacts import ArtifactStore
from takumi_research_agent.research.browser import SourceRetriever
from takumi_research_agent.research.evidence import EvidenceRegistrar
from takumi_research_agent.research.url_policy import PublicSourceUrlPolicy
from takumi_research_agent.security import SecretCipher
from takumi_research_agent.sources import LiteratureSources


class PipelineBlocked(RuntimeError):
    """Expected fail-closed conditions that should be visible as blocked runs."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class DailyResearchProcessor:
    """One bounded trend-to-draft run. Remote mutation is deliberately not part of this processor."""

    def __init__(self, sessions, settings: Settings):
        self.sessions = sessions
        self.settings = settings
        self.trends = TrendCollector()

    async def close(self) -> None:
        await self.trends.close()

    def _load_snapshot(self, run: Run) -> dict[str, Any]:
        snapshot = dict(run.config_snapshot or {})
        with self.sessions() as session:
            blog = session.get(BlogProfile, run.blog_id)
            provider = session.get(ProviderProfile, blog.provider_id) if blog and blog.provider_id else None
            control = session.get(GlobalSettings, 1)
            if blog is None or provider is None:
                raise PipelineBlocked("blog_or_provider_missing")
            if not blog.active or blog.revision != snapshot.get("blog_revision") or blog.destination_epoch != snapshot.get("destination_epoch"):
                raise PipelineBlocked("blog_configuration_changed_or_inactive")
            if provider.revision != snapshot.get("provider_revision") or not provider.active:
                raise PipelineBlocked("provider_configuration_changed_or_inactive")
            if provider.capability_result.get("status") != "reachable":
                raise PipelineBlocked("provider_text_probe_required")
            if not blog.capability_result.get("list_posts") or not blog.capability_result.get("history_complete"):
                raise PipelineBlocked("complete_blog_history_validation_required")
            if control is None or control.recovery_mode:
                raise PipelineBlocked("system_recovery_mode")
            if control.content_mutations_paused and run.execution_mode == "live":
                raise PipelineBlocked("global_content_pause_enabled")
            if snapshot.get("provider_id") != provider.id or snapshot.get("blog_id") != blog.id:
                raise PipelineBlocked("run_snapshot_mismatch")
            if not self.settings.master_key:
                raise PipelineBlocked("credential_encryption_key_missing")
            cipher = SecretCipher(self.settings.master_key)
            blog_token = cipher.decrypt(blog.encrypted_api_token) if blog.encrypted_api_token else ""
            provider_key = cipher.decrypt(provider.encrypted_api_key) if provider.encrypted_api_key else ""
            list_operation = blog.capabilities.get("list_operation")
            if not isinstance(list_operation, dict):
                raise PipelineBlocked("blog_list_operation_missing")
            try:
                mapping = BlogMapping.model_validate(blog.adapter_mapping)
                operation = Operation.model_validate(list_operation)
            except Exception as exc:
                raise PipelineBlocked("blog_adapter_configuration_invalid") from exc
            provider_config = ProviderConfig(
                model=provider.model,
                api_key=provider_key,
                base_url=provider.base_url,
                timeout_seconds=provider.timeout_seconds,
            )
            provider_client = AnthropicProvider(provider_config) if provider.provider_type == "anthropic" else OpenAICompatibleProvider(provider_config)
            blog_host = (urlsplit(blog.base_url).hostname or "").lower()
            adapter = BlogApiAdapter(
                base_url=blog.base_url,
                token=blog_token,
                mapping=mapping,
                list_operation=operation,
                auth_header=blog.auth_header,
                auth_scheme=blog.auth_scheme,
                capabilities=BlogCapabilities(list_posts=True),
                allow_insecure_private=blog_host in self.settings.allowed_private_hosts,
            )
            return {
                "provider": provider_client,
                "adapter": adapter,
                "topic_settings": dict(blog.topic_settings),
                "editorial_settings": dict(blog.editorial_settings),
                "capability_result": dict(blog.capability_result),
            }

    def _record_trends(self, candidates: list[TrendCandidate]) -> None:
        if not candidates:
            return
        unique: dict[tuple[str, str, datetime], TrendCandidate] = {}
        for item in candidates:
            unique.setdefault((item.source, item.source_item_id, item.observed_at), item)
        with self.sessions.begin() as session:
            for item in unique.values():
                existing = session.scalar(
                    select(TrendObservation).where(
                        TrendObservation.source == item.source,
                        TrendObservation.source_item_id == item.source_item_id,
                        TrendObservation.observed_at == item.observed_at,
                    )
                )
                if existing is not None:
                    continue
                session.add(
                    TrendObservation(
                        source=item.source,
                        source_item_id=item.source_item_id,
                        observed_at=item.observed_at,
                        published_at=item.published_at,
                        title=item.title[:1_000],
                        url=item.url[:2_000],
                        field=item.field,
                        raw_metrics=item.raw_metrics,
                        heuristic_score=item.score,
                        score_details=item.score_details,
                    )
                )

    def _resume_saved_pair(self, run: Run) -> dict[str, Any] | None:
        """Complete a reclaimed run from its atomic saved pair without repeating model/source work."""
        with self.sessions() as session:
            pair = session.scalar(
                select(ArticlePairRecord)
                .where(ArticlePairRecord.run_id == run.id)
                .order_by(ArticlePairRecord.created_at)
                .limit(1)
            )
            if pair is None:
                return None
            pair_id = pair.id
            decision = pair.decision

        delivery_note: dict[str, Any] = {"intents": 0}
        if decision in {"draft", "publish"}:
            try:
                with self.sessions.begin() as session:
                    created_intents = DeliveryService().prepare(session, run_id=run.id)
                    delivery_note["intents"] = len(created_intents)
                    if created_intents:
                        session.add(
                            RunEvent(
                                run_id=run.id,
                                event_type="delivery_prepared",
                                details={
                                    "intents": len(created_intents),
                                    "execution_mode": run.execution_mode,
                                    "recovered": True,
                                },
                            )
                        )
            except DeliveryBlocked as exc:
                delivery_note["blocked"] = exc.code
                with self.sessions.begin() as session:
                    session.add(
                        RunEvent(
                            run_id=run.id,
                            event_type="delivery_plan_skipped",
                            details={"code": exc.code, "detail": exc.detail[:300], "recovered": True},
                        )
                    )

        accepted = decision in {"draft", "publish"}
        target_at = run.target_at
        if target_at.tzinfo is None:
            target_at = target_at.replace(tzinfo=timezone.utc)
        return {
            "status": "completed",
            "reason": "draft_saved_locally" if accepted else "editorial_policy_blocked",
            "editorial_decision": decision,
            "pair_completeness": "complete" if accepted else "none",
            "deadline_result": "met" if datetime.now(timezone.utc) <= target_at.astimezone(timezone.utc) else "late",
            "pair_id": pair_id,
            "delivery": delivery_note,
            "note": "resumed_saved_article_without_repeating_research_or_remote_write",
        }

    def _sync_history(self, blog_id: str, posts: list[dict[str, Any]]) -> list[dict[str, Any]]:
        snapshot_id = _hash(f"{blog_id}:{datetime.now(timezone.utc).isoformat()}")[:36]
        normalized: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        with self.sessions.begin() as session:
            for post in posts:
                remote_id = str(post.get("id", "")).strip()
                title = str(post.get("title", "")).strip()
                if not remote_id or not title:
                    continue
                language = str(post.get("language") or "")[:10]
                content = str(post.get("content") or post.get("summary") or "")[:2_500]
                canonical = {**post, "id": remote_id, "title": title, "content": content}
                seen.add((remote_id, language))
                row = session.scalar(
                    select(BlogHistory).where(
                        BlogHistory.blog_id == blog_id,
                        BlogHistory.remote_id == remote_id,
                        BlogHistory.language == language,
                    )
                )
                if row is None:
                    row = BlogHistory(
                        blog_id=blog_id,
                        remote_id=remote_id,
                        language=language,
                        title=title,
                        content_hash=_hash(content) if content else None,
                        snapshot_id=snapshot_id,
                        canonical_record=canonical,
                    )
                    session.add(row)
                else:
                    row.title = title
                    row.snapshot_id = snapshot_id
                    row.canonical_record = canonical
                    row.content_hash = _hash(content) if content else None
                normalized.append(canonical)
            # A successful exhaustive GET represents the current remote history, so remove stale cache rows.
            for old in session.scalars(select(BlogHistory).where(BlogHistory.blog_id == blog_id)).all():
                if (old.remote_id, old.language) not in seen:
                    session.delete(old)
        return normalized

    def _persist_article(
        self,
        *,
        run_id: str,
        blog_id: str,
        pair,
        review,
        decision,
        final_bodies: dict[str, str],
        artifacts: ArtifactStore,
        trend: TrendCandidate,
        agent_metrics: dict[str, Any],
    ) -> None:
        with self.sessions.begin() as session:
            session.add(
                ArticlePairRecord(
                    id=pair.pair_id,
                    run_id=run_id,
                    topic=pair.topic,
                    field=pair.field,
                    research_question=pair.research_question,
                    decision=decision.decision,
                    review_result={
                        **review.model_dump(mode="json"),
                        "decision_reasons": decision.reasons,
                        "journal_support_sufficient": decision.journal_support_sufficient,
                        "novelty_reason": pair.novelty_reason,
                        "trend": {"source": trend.source, "score": trend.score, "score_details": trend.score_details},
                        "agent": agent_metrics,
                        # Frozen review inputs: delivery approval must break if evidence or sources change later.
                        "evidence": [
                            {"evidence_id": item.evidence_id, "source_id": item.source_id, "content_hash": item.content_hash}
                            for item in pair.evidence
                        ],
                        "sources": [
                            {
                                "source_id": source.source_id,
                                "content_hash": source.content_hash,
                                "read_status": source.read_status,
                                "retracted": source.retracted,
                            }
                            for source in pair.sources
                        ],
                        "claims": [claim.model_dump(mode="json") for claim in pair.claims],
                    },
                )
            )
            for source in pair.sources:
                row = session.get(SourceRecordRow, source.source_id)
                metadata = source.model_dump(mode="json")
                if row is None:
                    session.add(SourceRecordRow(id=source.source_id, url=str(source.url), title=source.title, source_type=source.source_type, metadata_json=metadata))
                else:
                    row.url = str(source.url)
                    row.title = source.title
                    row.source_type = source.source_type
                    row.metadata_json = metadata
            for item in pair.evidence:
                artifact = artifacts.root / "source" / item.content_hash[:2] / item.content_hash
                if session.get(EvidenceSpanRecord, item.evidence_id) is None:
                    session.add(
                        EvidenceSpanRecord(
                            id=item.evidence_id,
                            source_id=item.source_id,
                            content_hash=item.content_hash,
                            quote=item.quote,
                            locator=item.locator or "",
                            artifact_path=str(artifact),
                        )
                    )
            if decision.decision not in {"draft", "publish"}:
                session.add(
                    RunEvent(
                        run_id=run_id,
                        event_type="article_pair_blocked",
                        details={"pair_id": pair.pair_id, "decision": decision.decision, "reasons": decision.reasons},
                    )
                )
                return
            topic_fingerprint = _hash(pair.topic.casefold())
            for locale, article in pair.articles.items():
                final_body = final_bodies[locale]
                body_hash = _hash(final_body)
                session.add(
                    ArticleVersion(
                        pair_id=pair.pair_id,
                        locale=locale,
                        version=1,
                        title=article.title,
                        body_markdown=article.body_markdown,
                        final_body=final_body,
                        body_hash=body_hash,
                        metadata_json={
                            "summary": article.summary,
                            "format": "markdown",
                            "cited_evidence_ids": article.cited_evidence_ids,
                        },
                    )
                )
                session.add(
                    ContentLedger(
                        blog_id=blog_id,
                        pair_id=pair.pair_id,
                        topic_fingerprint=topic_fingerprint,
                        locale=locale,
                        unit_key=f"article:{locale}",
                        local_state="draft_saved",
                        remote_status="not_sent",
                        source_run_id=run_id,
                        metadata_json={"body_hash": body_hash, "editorial_decision": decision.decision},
                    )
                )
            session.add(RunEvent(run_id=run_id, event_type="article_pair_saved", details={"pair_id": pair.pair_id, "decision": decision.decision, "local_only": True}))

    async def process(self, run: Run) -> dict[str, Any]:
        resumed = self._resume_saved_pair(run)
        if resumed is not None:
            return resumed
        runtime = self._load_snapshot(run)
        provider = runtime["provider"]
        adapter = runtime["adapter"]
        literature = LiteratureSources(timezone=self.settings.timezone)
        policy = PublicSourceUrlPolicy(self.settings.allowed_domains)
        retriever = SourceRetriever(policy, browser_enabled=self.settings.headless)
        artifacts = ArtifactStore(self.settings.data_dir / "artifacts")
        budget = RequestBudget(max_requests=20, max_tool_actions=40)
        try:
            posts = await adapter.list_posts()
            history = self._sync_history(run.blog_id, posts)
            candidates = await self.trends.collect()
            self._record_trends(candidates)
            now = datetime.now(timezone.utc)
            enabled_fields = {field for field, enabled in runtime["topic_settings"].items() if enabled}
            candidates = [
                item for item in candidates
                if item.field in enabled_fields
                and item.published_at is not None
                and now - timedelta(hours=24) <= item.published_at.astimezone(timezone.utc) <= now + timedelta(minutes=10)
            ]
            if not candidates:
                raise PipelineBlocked("no_recent_trend_candidates")
            selected = candidates[0]
            toolbox = ResearchToolbox(
                provider=provider,
                budget=budget,
                literature=literature,
                retriever=retriever,
                artifacts=artifacts,
                blog_id=run.blog_id,
                blog_history=history,
                history_complete=bool(runtime["capability_result"].get("history_complete")),
            )
            research, agent_result = await toolbox.run(
                topic=selected.title,
                field=selected.field,
                trend_metrics={"source": selected.source, "score": selected.score, "score_details": selected.score_details, "raw": selected.raw_metrics},
            )
            if research is None:
                raise PipelineBlocked(f"research_{agent_result.status}_{agent_result.reason}")
            pair_id = _hash(f"{run.blog_id}:{run.id}:{research.output.topic}")[:64]
            lexical_scores = [item["similarity_hint"] for item in history_candidates(research.output.topic, history)]
            novelty_clear = research.output.novelty_clear and not any(score >= 0.30 for score in lexical_scores)
            if not novelty_clear:
                with self.sessions.begin() as session:
                    session.add(
                        RunEvent(
                            run_id=run.id,
                            event_type="candidate_skipped",
                            details={"reason": "novelty_unresolved", "topic_fingerprint": _hash(research.output.topic.casefold())},
                        )
                    )
                return {
                    "status": "completed",
                    "reason": "candidate_skipped_novelty_unresolved",
                    "editorial_decision": "skip",
                    "pair_completeness": "none",
                    "deadline_result": "met" if datetime.now(timezone.utc) <= run.target_at.replace(tzinfo=timezone.utc) else "late",
                }
            editorial = EditorialService(provider, budget, evidence_registrar=EvidenceRegistrar(artifacts))
            pair, review, final_bodies = await editorial.write_and_review(
                pair_id=pair_id,
                field=research.output.field,
                topic=research.output.topic,
                research_question=research.output.research_question,
                findings=research.output.findings,
                sources=research.sources,
                evidence=research.evidence,
                claims=research.output.claims,
                novelty_reason=research.output.novelty_reason,
                output_format="markdown",
            )
            credible = any(
                source.source_type in {"journal", "official", "dataset", "preprint"}
                and not source.retracted
                and source.read_status in {"abstract", "full_text"}
                for source in pair.sources
            )
            decision = editorial_decision(pair, review=review, novelty_clear=novelty_clear, credible_sources=credible)
            self._persist_article(
                run_id=run.id,
                blog_id=run.blog_id,
                pair=pair,
                review=review,
                decision=decision,
                final_bodies=final_bodies,
                artifacts=artifacts,
                trend=selected,
                agent_metrics={
                    "physical_requests": budget.physical_requests,
                    "research_physical_requests": agent_result.physical_requests,
                    "editorial_physical_requests": budget.physical_requests - agent_result.physical_requests,
                    "tool_actions": budget.tool_actions,
                    "trace": list(agent_result.trace),
                },
            )
            delivery_note: dict[str, Any] = {"intents": 0}
            try:
                with self.sessions.begin() as session:
                    created_intents = DeliveryService().prepare(session, run_id=run.id)
                    delivery_note["intents"] = len(created_intents)
                    session.add(
                        RunEvent(
                            run_id=run.id,
                            event_type="delivery_prepared",
                            details={"intents": len(created_intents), "execution_mode": run.execution_mode},
                        )
                    )
            except DeliveryBlocked as exc:
                delivery_note["blocked"] = exc.code
                with self.sessions.begin() as session:
                    session.add(RunEvent(run_id=run.id, event_type="delivery_plan_skipped", details={"code": exc.code, "detail": exc.detail[:300]}))
            return {
                "status": "completed",
                "reason": "draft_saved_locally" if decision.decision in {"draft", "publish"} else "editorial_policy_blocked",
                "editorial_decision": decision.decision,
                "pair_completeness": "complete" if decision.decision in {"draft", "publish"} else "none",
                "deadline_result": "met" if datetime.now(timezone.utc) <= run.target_at.replace(tzinfo=timezone.utc) else "late",
                "pair_id": pair_id,
                "delivery": delivery_note,
                "note": "remote_blog_write_not_validated",
            }
        finally:
            await provider.close()
            await adapter.close()
            await literature.close()
            await retriever.close()
