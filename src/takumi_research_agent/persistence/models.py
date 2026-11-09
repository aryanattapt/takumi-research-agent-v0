from __future__ import annotations

from datetime import date, datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import Boolean, CheckConstraint, Date, DateTime, ForeignKey, Index, Integer, JSON, String, Text, UniqueConstraint, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def uid() -> str:
    return str(uuid4())


class Base(DeclarativeBase):
    pass


class AdminUser(Base):
    __tablename__ = "admin_users"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    username: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(300), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class AdminSession(Base):
    __tablename__ = "admin_sessions"
    id_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("admin_users.id", ondelete="CASCADE"), nullable=False)
    csrf_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class GlobalSettings(Base):
    __tablename__ = "global_settings"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    recovery_mode: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    content_mutations_paused: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    revision: Mapped[int] = mapped_column(Integer, default=1, nullable=False)


class ProviderProfile(Base):
    __tablename__ = "provider_profiles"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    provider_type: Mapped[str] = mapped_column(String(40), nullable=False)
    base_url: Mapped[str] = mapped_column(String(1_000), nullable=False)
    model: Mapped[str] = mapped_column(String(200), nullable=False)
    encrypted_api_key: Mapped[str] = mapped_column(Text, nullable=False)
    timeout_seconds: Mapped[int] = mapped_column(Integer, default=90, nullable=False)
    max_output_tokens: Mapped[int] = mapped_column(Integer, default=4_000, nullable=False)
    capability_result: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    revision: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    __table_args__ = (CheckConstraint("provider_type in ('openai_compatible', 'anthropic')", name="ck_provider_type"),)


class BlogProfile(Base):
    __tablename__ = "blog_profiles"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    base_url: Mapped[str] = mapped_column(String(1_000), nullable=False)
    api_spec_source: Mapped[str] = mapped_column(String(2_000), nullable=False, default="")
    auth_header: Mapped[str] = mapped_column(String(100), default="Authorization", nullable=False)
    auth_scheme: Mapped[str] = mapped_column(String(60), default="Bearer", nullable=False)
    encrypted_api_token: Mapped[str] = mapped_column(Text, nullable=False, default="")
    provider_id: Mapped[str | None] = mapped_column(ForeignKey("provider_profiles.id", ondelete="SET NULL"))
    adapter_mapping: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    capabilities: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    capability_result: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    validated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    validated_revision: Mapped[int | None] = mapped_column(Integer)
    topic_settings: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    editorial_settings: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    schedule_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    schedule_timezone: Mapped[str] = mapped_column(String(100), default="Asia/Jakarta", nullable=False)
    publish_time: Mapped[str] = mapped_column(String(5), default="06:00", nullable=False)
    lead_minutes: Mapped[int] = mapped_column(Integer, default=120, nullable=False)
    revision: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    destination_epoch: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    provider: Mapped[ProviderProfile | None] = relationship()
    __table_args__ = (
        CheckConstraint("lead_minutes between 0 and 1440", name="ck_blog_lead_minutes"),
        CheckConstraint("length(publish_time) = 5", name="ck_blog_publish_time"),
        Index("ix_blog_profiles_active", "active"),
    )


class AdapterVersion(Base):
    __tablename__ = "adapter_versions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    blog_id: Mapped[str] = mapped_column(ForeignKey("blog_profiles.id", ondelete="CASCADE"), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    schema_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    mapping: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    capability_result: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    validated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    __table_args__ = (UniqueConstraint("blog_id", "revision", name="uq_adapter_blog_revision"),)


class BlogHistory(Base):
    __tablename__ = "blog_history"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    blog_id: Mapped[str] = mapped_column(ForeignKey("blog_profiles.id", ondelete="CASCADE"), nullable=False)
    remote_id: Mapped[str] = mapped_column(String(300), nullable=False)
    language: Mapped[str] = mapped_column(String(10), default="", nullable=False)
    title: Mapped[str] = mapped_column(String(1_000), nullable=False)
    content_hash: Mapped[str | None] = mapped_column(String(64))
    canonical_record: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    snapshot_id: Mapped[str] = mapped_column(String(36), nullable=False)
    synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    __table_args__ = (UniqueConstraint("blog_id", "remote_id", "language", name="uq_blog_history_remote_locale"),)


class Run(Base):
    __tablename__ = "runs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    blog_id: Mapped[str] = mapped_column(ForeignKey("blog_profiles.id", ondelete="CASCADE"), nullable=False)
    editorial_date: Mapped[date] = mapped_column(Date, nullable=False)
    quota_ordinal: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    quota_slot_key: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    target_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(40), default="queued", nullable=False)
    stage: Mapped[str] = mapped_column(String(40), default="queued", nullable=False)
    execution_mode: Mapped[str] = mapped_column(String(20), default="dryrun", nullable=False)
    config_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    pipeline_outcome: Mapped[str] = mapped_column(String(40), default="queued", nullable=False)
    editorial_decision: Mapped[str] = mapped_column(String(20), default="pending", nullable=False)
    pair_completeness: Mapped[str] = mapped_column(String(20), default="none", nullable=False)
    deadline_result: Mapped[str] = mapped_column(String(20), default="pending", nullable=False)
    # Deadline admission control (orchestration/capacity.py): schedule version frozen at enqueue,
    # the latest honest start that still meets target_at, and the current capacity verdict.
    schedule_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    capacity_state: Mapped[str] = mapped_column(String(20), default="ok", nullable=False)
    start_by_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_owner: Mapped[str | None] = mapped_column(String(120))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_epoch: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    state_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    next_run_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    __table_args__ = (
        UniqueConstraint("blog_id", "editorial_date", "quota_ordinal", name="uq_run_daily_quota"),
        CheckConstraint("execution_mode in ('dryrun', 'sandbox', 'live')", name="ck_run_mode"),
        CheckConstraint("capacity_state in ('ok', 'at_risk', 'infeasible')", name="ck_run_capacity_state"),
        Index("ix_runs_claim", "status", "next_run_at", "lease_until"),
    )


class ContentLedger(Base):
    __tablename__ = "content_ledger"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    blog_id: Mapped[str] = mapped_column(ForeignKey("blog_profiles.id", ondelete="CASCADE"), nullable=False)
    pair_id: Mapped[str] = mapped_column(String(120), nullable=False)
    topic_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    locale: Mapped[str] = mapped_column(String(10), default="", nullable=False)
    unit_key: Mapped[str] = mapped_column(String(120), nullable=False)
    local_state: Mapped[str] = mapped_column(String(40), default="prepared", nullable=False)
    remote_id: Mapped[str | None] = mapped_column(String(300))
    remote_url: Mapped[str | None] = mapped_column(String(2_000))
    remote_status: Mapped[str] = mapped_column(String(40), default="unknown", nullable=False)
    remote_published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source_run_id: Mapped[str | None] = mapped_column(ForeignKey("runs.id", ondelete="SET NULL"))
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
    __table_args__ = (
        UniqueConstraint("blog_id", "pair_id", "unit_key", name="uq_content_ledger_unit"),
        Index("ix_content_ledger_topic", "blog_id", "topic_fingerprint"),
    )


class DeliveryIntent(Base):
    __tablename__ = "delivery_intents"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), nullable=False)
    blog_id: Mapped[str] = mapped_column(ForeignKey("blog_profiles.id", ondelete="CASCADE"), nullable=False)
    pair_id: Mapped[str] = mapped_column(String(120), nullable=False)
    unit_key: Mapped[str] = mapped_column(String(120), nullable=False)
    action: Mapped[str] = mapped_column(String(40), nullable=False)
    operation_id: Mapped[str] = mapped_column(String(120), nullable=False)
    destination_epoch: Mapped[int] = mapped_column(Integer, nullable=False)
    delivery_key: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    approval_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    state: Mapped[str] = mapped_column(String(40), default="prepared", nullable=False)
    remote_id: Mapped[str | None] = mapped_column(String(300))
    response_summary: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class DeliveryAttempt(Base):
    __tablename__ = "delivery_attempts"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    intent_id: Mapped[str] = mapped_column(ForeignKey("delivery_intents.id", ondelete="CASCADE"), nullable=False)
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    outcome: Mapped[str] = mapped_column(String(40), default="in_flight", nullable=False)
    http_status: Mapped[int | None] = mapped_column(Integer)
    response_summary: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    __table_args__ = (UniqueConstraint("intent_id", "attempt_number", name="uq_delivery_attempt"),)


class TopicReservation(Base):
    __tablename__ = "topic_reservations"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    blog_id: Mapped[str] = mapped_column(ForeignKey("blog_profiles.id", ondelete="CASCADE"), nullable=False)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), nullable=False)
    topic_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(30), default="reserved", nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    __table_args__ = (UniqueConstraint("blog_id", "topic_fingerprint", "state", name="uq_topic_active_reservation"),)


class TrendObservation(Base):
    __tablename__ = "trend_observations"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    source: Mapped[str] = mapped_column(String(120), nullable=False)
    source_item_id: Mapped[str] = mapped_column(String(300), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    title: Mapped[str] = mapped_column(String(1_000), nullable=False)
    url: Mapped[str] = mapped_column(String(2_000), nullable=False)
    field: Mapped[str] = mapped_column(String(40), nullable=False)
    raw_metrics: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    heuristic_score: Mapped[float | None] = mapped_column()
    score_details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    __table_args__ = (UniqueConstraint("source", "source_item_id", "observed_at", name="uq_trend_observation"),)


class SourceRecord(Base):
    __tablename__ = "sources"
    id: Mapped[str] = mapped_column(String(180), primary_key=True)
    url: Mapped[str] = mapped_column(String(2_000), nullable=False)
    title: Mapped[str] = mapped_column(String(1_000), nullable=False)
    source_type: Mapped[str] = mapped_column(String(40), nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class EvidenceSpanRecord(Base):
    __tablename__ = "evidence_spans"
    id: Mapped[str] = mapped_column(String(220), primary_key=True)
    source_id: Mapped[str] = mapped_column(ForeignKey("sources.id", ondelete="CASCADE"), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    quote: Mapped[str] = mapped_column(Text, nullable=False)
    locator: Mapped[str] = mapped_column(String(300), default="", nullable=False)
    artifact_path: Mapped[str] = mapped_column(String(2_000), default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class ArticlePairRecord(Base):
    __tablename__ = "article_pairs"
    id: Mapped[str] = mapped_column(String(120), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), nullable=False)
    topic: Mapped[str] = mapped_column(String(1_000), nullable=False)
    field: Mapped[str] = mapped_column(String(40), nullable=False)
    research_question: Mapped[str] = mapped_column(Text, nullable=False)
    decision: Mapped[str] = mapped_column(String(20), default="pending", nullable=False)
    review_result: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class ArticleVersion(Base):
    __tablename__ = "article_versions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    pair_id: Mapped[str] = mapped_column(ForeignKey("article_pairs.id", ondelete="CASCADE"), nullable=False)
    locale: Mapped[str] = mapped_column(String(10), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(String(1_000), nullable=False)
    body_markdown: Mapped[str] = mapped_column(Text, nullable=False)
    final_body: Mapped[str] = mapped_column(Text, nullable=False)
    body_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    __table_args__ = (UniqueConstraint("pair_id", "locale", "version", name="uq_article_version"),)


class RunEvent(Base):
    __tablename__ = "run_events"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), nullable=False)
    event_type: Mapped[str] = mapped_column(String(80), nullable=False)
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    __table_args__ = (Index("ix_run_events_run_created", "run_id", "created_at"),)


class AuditEvent(Base):
    __tablename__ = "audit_events"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    actor_user_id: Mapped[str | None] = mapped_column(ForeignKey("admin_users.id", ondelete="SET NULL"))
    action: Mapped[str] = mapped_column(String(100), nullable=False)
    resource_type: Mapped[str] = mapped_column(String(80), nullable=False)
    resource_id: Mapped[str] = mapped_column(String(120), nullable=False)
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class LegacyImportRecord(Base):
    """Idempotency marker for the legacy SQLite import; one row per imported legacy record."""

    __tablename__ = "legacy_import_records"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    source_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    source_key: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    table_name: Mapped[str] = mapped_column(String(40), nullable=False)
    record_key: Mapped[str] = mapped_column(String(400), nullable=False)
    target_type: Mapped[str] = mapped_column(String(60), nullable=False)
    target_id: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    imported_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    __table_args__ = (UniqueConstraint("source_fingerprint", "table_name", "record_key", name="uq_legacy_import_record"),)
