from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select, update

from takumi_research_agent.blogs.adapter import BlogApiAdapter, DeliveryOutcomeUnknown
from takumi_research_agent.blogs.contracts import BlogMapping, ContractError, Operation, build_create_payload, get_path
from takumi_research_agent.delivery.keys import approval_fingerprint, delivery_key
from takumi_research_agent.persistence.models import (
    ArticlePairRecord,
    ArticleVersion,
    BlogProfile,
    ContentLedger,
    DeliveryAttempt,
    DeliveryIntent,
    GlobalSettings,
    Run,
    RunEvent,
)


MAX_ATTEMPTS = 3
DELIVERABLE_STATES = ("prepared", "failed", "dispatching")
ACTION_FOR_DECISION = {"draft": "draft", "publish": "publish"}


class DeliveryBlocked(RuntimeError):
    """A fail-closed delivery condition that must stay visible to the operator."""

    def __init__(self, code: str, detail: str = ""):
        super().__init__(detail or code)
        self.code = code
        self.detail = detail


@dataclass(frozen=True, slots=True)
class DeliveryUnit:
    unit_key: str
    locale: str
    action: str
    operation_id: str
    payload: dict[str, Any]
    delivery_key: str
    approval_fingerprint: str


@dataclass(slots=True)
class DispatchSummary:
    succeeded: int = 0
    failed: int = 0
    unknown: int = 0
    blocked: list[dict[str, str]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "succeeded": self.succeeded,
            "failed": self.failed,
            "unknown": self.unknown,
            "blocked": self.blocked,
        }


def _digest(value: Any) -> str:
    body = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _canonical(version: ArticleVersion, action: str) -> dict[str, Any]:
    return {
        "title": version.title,
        "content": version.final_body,
        "summary": str(version.metadata_json.get("summary", "")),
        "locale": version.locale,
        "language": version.locale,
        "status": action,
    }


def _adapter_contract(blog: BlogProfile) -> tuple[BlogMapping, Operation]:
    try:
        mapping = BlogMapping.model_validate(blog.adapter_mapping)
        raw_operation = (blog.capabilities or {}).get("create_operation")
        if not isinstance(raw_operation, dict):
            raise DeliveryBlocked("delivery_create_operation_unconfigured")
        operation = Operation.model_validate(raw_operation)
    except DeliveryBlocked:
        raise
    except (ContractError, ValueError) as exc:
        raise DeliveryBlocked("delivery_adapter_configuration_invalid", str(exc)[:300]) from exc
    return mapping, operation


def _material(
    *,
    run: Run,
    blog: BlogProfile,
    pair: ArticlePairRecord,
    version: ArticleVersion,
    mapping: BlogMapping,
    operation: Operation,
    action: str,
    unit_key: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Everything that freezes a prepared mutation to the reviewed content and destination."""
    return {
        "blog_id": blog.id,
        "blog_revision": blog.revision,
        "destination_epoch": blog.destination_epoch,
        "execution_mode": run.execution_mode,
        "adapter": _digest({"base_url": blog.base_url, "mapping": mapping.model_dump(mode="json"), "capabilities": blog.capabilities}),
        "editorial_settings": _digest(blog.editorial_settings),
        "schedule": _digest(
            {
                "enabled": blog.schedule_enabled,
                "timezone": blog.schedule_timezone,
                "publish_time": blog.publish_time,
                "lead_minutes": blog.lead_minutes,
            }
        ),
        "operation_id": operation.operation_id,
        "operation_method": operation.method,
        "operation_path": operation.path,
        "pair_id": pair.id,
        "decision": pair.decision,
        "review": _digest(pair.review_result),
        "locale": version.locale,
        "unit_key": unit_key,
        "action": action,
        "body_hash": version.body_hash,
        "payload": _digest(payload),
    }


def _unit_for(
    *,
    run: Run,
    blog: BlogProfile,
    pair: ArticlePairRecord,
    version: ArticleVersion,
    mapping: BlogMapping,
    operation: Operation,
    action: str,
) -> DeliveryUnit:
    unit_key = f"article:{version.locale}"
    payload = build_create_payload(_canonical(version, action), mapping)
    return DeliveryUnit(
        unit_key=unit_key,
        locale=version.locale,
        action=action,
        operation_id=operation.operation_id,
        payload=payload,
        delivery_key=delivery_key(
            blog_id=blog.id,
            destination_epoch=blog.destination_epoch,
            pair_id=pair.id,
            unit_key=unit_key,
            action=action,
            operation_id=operation.operation_id,
        ),
        approval_fingerprint=approval_fingerprint(
            _material(
                run=run,
                blog=blog,
                pair=pair,
                version=version,
                mapping=mapping,
                operation=operation,
                action=action,
                unit_key=unit_key,
                payload=payload,
            )
        ),
    )


class DeliveryService:
    """Local outbox for reviewed content; every remote mutation stays capability- and approval-gated."""

    def __init__(self, *, max_attempts: int = MAX_ATTEMPTS):
        self.max_attempts = max_attempts

    def prepare(self, session, *, run_id: str) -> list[DeliveryIntent]:
        run = session.get(Run, run_id)
        if run is None:
            raise DeliveryBlocked("run_missing")
        pair = session.scalar(select(ArticlePairRecord).where(ArticlePairRecord.run_id == run_id))
        if pair is None or pair.decision not in ACTION_FOR_DECISION:
            return []
        blog = session.get(BlogProfile, run.blog_id)
        if blog is None:
            raise DeliveryBlocked("blog_missing")
        mapping, operation = _adapter_contract(blog)
        action = ACTION_FOR_DECISION[pair.decision]
        versions = session.scalars(
            select(ArticleVersion).where(ArticleVersion.pair_id == pair.id).order_by(ArticleVersion.locale)
        ).all()
        if {version.locale for version in versions} != {"id", "en"}:
            raise DeliveryBlocked("article_pair_incomplete")
        created: list[DeliveryIntent] = []
        for version in versions:
            try:
                unit = _unit_for(run=run, blog=blog, pair=pair, version=version, mapping=mapping, operation=operation, action=action)
            except ContractError as exc:
                raise DeliveryBlocked("delivery_payload_mapping_failed", str(exc)[:300]) from exc
            existing = session.scalar(select(DeliveryIntent).where(DeliveryIntent.delivery_key == unit.delivery_key))
            if existing is not None:
                continue
            intent = DeliveryIntent(
                run_id=run.id,
                blog_id=blog.id,
                pair_id=pair.id,
                unit_key=unit.unit_key,
                action=unit.action,
                operation_id=unit.operation_id,
                destination_epoch=blog.destination_epoch,
                delivery_key=unit.delivery_key,
                approval_fingerprint=unit.approval_fingerprint,
                payload=unit.payload,
                state="prepared",
            )
            session.add(intent)
            created.append(intent)
            ledger = session.scalar(
                select(ContentLedger).where(
                    ContentLedger.blog_id == blog.id,
                    ContentLedger.pair_id == pair.id,
                    ContentLedger.unit_key == unit.unit_key,
                )
            )
            if ledger is not None:
                ledger.local_state = "delivery_prepared"
        session.flush()
        return created

    def _capability_block(self, blog: BlogProfile, action: str) -> str | None:
        if action == "publish" and not blog.capability_result.get("publish"):
            return "publish_capability_unproven"
        if action == "draft" and not blog.capability_result.get("create_draft"):
            return "create_draft_capability_unproven"
        return None

    async def dispatch(self, session, *, run_id: str, adapter: BlogApiAdapter) -> dict[str, Any]:
        run = session.get(Run, run_id)
        if run is None:
            raise DeliveryBlocked("run_missing")
        blog = session.get(BlogProfile, run.blog_id)
        if blog is None:
            raise DeliveryBlocked("blog_missing")
        control = session.get(GlobalSettings, 1)
        if run.execution_mode == "dryrun":
            raise DeliveryBlocked("dryrun_never_delivers")
        if control is None:
            raise DeliveryBlocked("global_settings_missing")
        if control.recovery_mode:
            raise DeliveryBlocked("system_recovery_mode")
        if control.content_mutations_paused:
            raise DeliveryBlocked("content_mutations_paused")
        snapshot = run.config_snapshot or {}
        if (
            not blog.active
            or blog.revision != snapshot.get("blog_revision")
            or blog.destination_epoch != snapshot.get("destination_epoch")
        ):
            raise DeliveryBlocked("blog_configuration_changed_or_inactive")
        mapping, operation = _adapter_contract(blog)
        pair = session.scalar(select(ArticlePairRecord).where(ArticlePairRecord.run_id == run_id))
        if pair is None:
            raise DeliveryBlocked("article_pair_missing")
        versions = {
            version.locale: version
            for version in session.scalars(select(ArticleVersion).where(ArticleVersion.pair_id == pair.id))
        }
        if set(versions) != {"id", "en"}:
            raise DeliveryBlocked("article_pair_incomplete")

        summary = DispatchSummary()
        intents = session.scalars(
            select(DeliveryIntent)
            .where(DeliveryIntent.run_id == run_id, DeliveryIntent.state.in_(DELIVERABLE_STATES))
            .order_by(DeliveryIntent.unit_key)
        ).all()
        for intent in intents:
            if intent.state == "dispatching":
                if self._finish_unknown(session, intent, reason="stale_dispatching_state"):
                    summary.unknown += 1
                continue
            capability_block = self._capability_block(blog, intent.action)
            if capability_block:
                self._record_blocked(session, run.id, intent, capability_block)
                summary.blocked.append({"unit_key": intent.unit_key, "code": capability_block})
                continue
            version = versions.get(intent.unit_key.removeprefix("article:"))
            if version is None:
                self._record_blocked(session, run.id, intent, "article_version_missing")
                summary.blocked.append({"unit_key": intent.unit_key, "code": "article_version_missing"})
                continue
            try:
                unit = _unit_for(
                    run=run,
                    blog=blog,
                    pair=pair,
                    version=version,
                    mapping=mapping,
                    operation=operation,
                    action=intent.action,
                )
            except ContractError as exc:
                self._record_blocked(session, run.id, intent, f"delivery_payload_mapping_failed: {str(exc)[:200]}")
                summary.blocked.append({"unit_key": intent.unit_key, "code": "delivery_payload_mapping_failed"})
                continue
            if unit.delivery_key != intent.delivery_key or unit.approval_fingerprint != intent.approval_fingerprint:
                intent.state = "invalidated"
                self._record_event(session, run.id, "delivery_approval_invalidated", {"intent_id": intent.id, "unit_key": intent.unit_key})
                summary.blocked.append({"unit_key": intent.unit_key, "code": "approval_invalidated"})
                continue
            attempt_number = (session.scalar(select(func.max(DeliveryAttempt.attempt_number)).where(DeliveryAttempt.intent_id == intent.id)) or 0) + 1
            if attempt_number > self.max_attempts:
                self._record_blocked(session, run.id, intent, "delivery_attempt_limit_reached")
                summary.blocked.append({"unit_key": intent.unit_key, "code": "delivery_attempt_limit_reached"})
                continue
            claimed = session.execute(
                update(DeliveryIntent)
                .where(DeliveryIntent.id == intent.id, DeliveryIntent.state.in_(("prepared", "failed")))
                .values(state="dispatching")
            ).rowcount
            if claimed != 1:
                continue
            attempt = DeliveryAttempt(intent_id=intent.id, attempt_number=attempt_number, outcome="in_flight")
            session.add(attempt)
            session.commit()

            try:
                result = await adapter.send_payload(
                    intent.payload,
                    intent_key=f"takumi:{intent.delivery_key}",
                    publish=intent.action == "publish",
                )
            except DeliveryOutcomeUnknown as exc:
                if self._finish_unknown(session, intent, attempt=attempt, reason=str(exc)[:300]):
                    summary.unknown += 1
            except Exception as exc:  # record the category only; exception text may carry headers.
                status = getattr(getattr(exc, "response", None), "status_code", None)
                if self._finish_failed(session, intent, attempt=attempt, http_status=status, reason=type(exc).__name__):
                    summary.failed += 1
            else:
                remote_id = self._remote_id(mapping, result.get("body"))
                if self._finish_succeeded(session, intent, attempt=attempt, http_status=result.get("http_status"), remote_id=remote_id):
                    summary.succeeded += 1
        session.commit()
        return summary.as_dict()

    @staticmethod
    def _remote_id(mapping: BlogMapping, body: Any) -> str | None:
        path = mapping.response_fields.get("id")
        if not path or not isinstance(body, dict):
            return None
        value = get_path(body, path)
        if value in (None, ""):
            return None
        return str(value)[:300]

    def _record_event(self, session, run_id: str, event_type: str, details: dict[str, Any]) -> None:
        session.add(RunEvent(run_id=run_id, event_type=event_type, details=details))

    def _record_blocked(self, session, run_id: str, intent: DeliveryIntent, code: str) -> None:
        self._record_event(session, run_id, "delivery_blocked", {"intent_id": intent.id, "unit_key": intent.unit_key, "code": code})

    def _ledger(self, session, intent: DeliveryIntent) -> ContentLedger | None:
        return session.scalar(
            select(ContentLedger).where(
                ContentLedger.blog_id == intent.blog_id,
                ContentLedger.pair_id == intent.pair_id,
                ContentLedger.unit_key == intent.unit_key,
            )
        )

    @staticmethod
    def _transition(session, intent: DeliveryIntent, *, state: str, summary: dict[str, Any], remote_id: str | None = None) -> bool:
        """Finalize only the attempt that still owns the dispatching state."""
        values: dict[str, Any] = {"state": state, "response_summary": summary}
        if remote_id is not None:
            values["remote_id"] = remote_id
        result = session.execute(
            update(DeliveryIntent)
            .where(DeliveryIntent.id == intent.id, DeliveryIntent.state == "dispatching")
            .values(**values)
            .execution_options(synchronize_session=False)
        )
        return result.rowcount == 1

    def _finish_succeeded(self, session, intent: DeliveryIntent, *, attempt: DeliveryAttempt, http_status: int | None, remote_id: str | None) -> bool:
        now = datetime.now(timezone.utc)
        attempt.finished_at = now
        attempt.outcome = "succeeded"
        attempt.http_status = http_status
        attempt.response_summary = {"http_status": http_status, "remote_id": remote_id}
        if not self._transition(
            session,
            intent,
            state="succeeded",
            summary={"http_status": http_status, "remote_id": remote_id, "verified": False},
            remote_id=remote_id,
        ):
            self._record_event(session, intent.run_id, "delivery_finalize_skipped", {"intent_id": intent.id, "unit_key": intent.unit_key, "attempted_state": "succeeded"})
            return False
        ledger = self._ledger(session, intent)
        if ledger is not None:
            ledger.local_state = "delivered"
            ledger.remote_id = remote_id
            ledger.remote_status = "accepted_unverified"
            ledger.verified_at = now
            ledger.metadata_json = {**ledger.metadata_json, "delivery_key": intent.delivery_key}
        self._record_event(session, intent.run_id, "delivery_succeeded", {"intent_id": intent.id, "unit_key": intent.unit_key, "http_status": http_status})
        return True

    def _finish_failed(self, session, intent: DeliveryIntent, *, attempt: DeliveryAttempt | None, http_status: int | None, reason: str) -> bool:
        if attempt is not None:
            attempt.finished_at = datetime.now(timezone.utc)
            attempt.outcome = "failed"
            attempt.http_status = http_status
            attempt.response_summary = {"http_status": http_status, "error": reason[:300]}
        if not self._transition(session, intent, state="failed", summary={"http_status": http_status, "error": reason[:300]}):
            self._record_event(session, intent.run_id, "delivery_finalize_skipped", {"intent_id": intent.id, "unit_key": intent.unit_key, "attempted_state": "failed"})
            return False
        self._record_event(session, intent.run_id, "delivery_failed", {"intent_id": intent.id, "unit_key": intent.unit_key, "http_status": http_status, "error": reason[:200]})
        return True

    def _finish_unknown(self, session, intent: DeliveryIntent, *, attempt: DeliveryAttempt | None = None, reason: str) -> bool:
        now = datetime.now(timezone.utc)
        if attempt is not None:
            attempt.finished_at = now
            attempt.outcome = "unknown"
            attempt.response_summary = {"error": reason[:300]}
        if not self._transition(session, intent, state="unknown", summary={"error": reason[:300], "reconciliation_required": True}):
            self._record_event(session, intent.run_id, "delivery_finalize_skipped", {"intent_id": intent.id, "unit_key": intent.unit_key, "attempted_state": "unknown"})
            return False
        ledger = self._ledger(session, intent)
        if ledger is not None:
            ledger.local_state = "delivery_unknown"
            ledger.remote_status = "unknown"
        self._record_event(session, intent.run_id, "delivery_unknown", {"intent_id": intent.id, "unit_key": intent.unit_key, "error": reason[:200]})
        return True
