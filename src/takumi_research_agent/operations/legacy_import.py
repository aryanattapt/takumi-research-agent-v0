"""One-way import of the legacy single-blog SQLite file into the durable schema (plan §12.6).

The legacy store (``takumi_research_agent.store``) kept one flat blog's papers, posts,
drafts, jobs, publications, and agent events in ``DATA_DIR/takumi.sqlite3``. This importer
maps what maps cleanly (papers -> sources, posts -> blog history, publications -> content
ledger) and preserves the rest as immutable artifacts plus audit events, because a legacy
draft bundle has no run/pair identity to attach to.

Rules enforced here:

* the legacy file is opened read-only and never modified;
* every imported row goes to exactly one blog profile, chosen by the operator;
* the target profile must be inactive, so imported publications are reconciled before a
  schedule can produce remote writes again;
* re-running is safe: each legacy record is marked once in ``legacy_import_records``.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from takumi_research_agent.persistence.models import (
    AuditEvent,
    BlogHistory,
    BlogProfile,
    ContentLedger,
    LegacyImportRecord,
    SourceRecord,
)
from takumi_research_agent.research.artifacts import ArtifactStore

LEGACY_TABLES = ("papers", "blog_posts", "drafts", "publications", "jobs", "agent_events")
SOURCE_TYPES = {"journal", "preprint", "official", "dataset", "press"}
MAX_REPORT_SKIPS = 200


class LegacyImportBlocked(RuntimeError):
    def __init__(self, code: str, detail: str = ""):
        super().__init__(code)
        self.code = code
        self.detail = detail


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _parse_json(value: Any) -> Any:
    if value in (None, ""):
        return None
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return None


def _read_legacy_rows(path: Path) -> dict[str, list[sqlite3.Row]]:
    # Read-only URI: the legacy file must stay byte-identical for rollback and comparison.
    connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        rows: dict[str, list[sqlite3.Row]] = {}
        for table in LEGACY_TABLES:
            try:
                rows[table] = list(connection.execute(f"select * from {table}").fetchall())
            except sqlite3.OperationalError:
                rows[table] = []
        return rows
    finally:
        connection.close()


def _marker(session: Session, fingerprint: str, table: str, key: str) -> LegacyImportRecord | None:
    return session.scalar(
        select(LegacyImportRecord).where(
            LegacyImportRecord.source_fingerprint == fingerprint,
            LegacyImportRecord.table_name == table,
            LegacyImportRecord.record_key == key,
        )
    )


class _Counters:
    def __init__(self) -> None:
        self.counts: dict[str, dict[str, int]] = {}
        self.skipped: list[dict[str, str]] = []
        self.artifacts_written = 0

    def count(self, table: str, outcome: str) -> None:
        bucket = self.counts.setdefault(table, {"imported": 0, "duplicates": 0, "skipped": 0})
        bucket[outcome] += 1

    def skip(self, table: str, key: str, reason: str) -> None:
        self.count(table, "skipped")
        if len(self.skipped) < MAX_REPORT_SKIPS:
            self.skipped.append({"table": table, "key": key[:200], "reason": reason})

    def as_dict(self) -> dict[str, dict[str, int]]:
        return {table: dict(self.counts.get(table, {"imported": 0, "duplicates": 0, "skipped": 0})) for table in LEGACY_TABLES}


def import_legacy_sqlite(
    session: Session,
    *,
    database_path: Path,
    blog_id: str,
    artifacts: ArtifactStore | None = None,
    dry_run: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    path = Path(database_path)
    if not path.is_file():
        raise LegacyImportBlocked("legacy_database_missing", str(path))
    blog = session.get(BlogProfile, blog_id)
    if blog is None:
        raise LegacyImportBlocked("blog_profile_not_found", blog_id)
    if blog.active:
        raise LegacyImportBlocked("blog_profile_must_be_inactive", "deactivate the profile, import, then reconcile before activating")
    imported_at = now or datetime.now(timezone.utc)
    fingerprint = _hash(f"legacy-sqlite:{path.name}")
    source_key = path.name[:200]
    file_digest = _sha256_file(path)
    rows = _read_legacy_rows(path)
    counters = _Counters()
    snapshot_id = _hash(f"legacy:{fingerprint}")[:36]
    reconciliation: list[str] = []
    imported_artifacts: set[str] = set()
    # A dry run writes inside a savepoint so the report still counts what *would* happen
    # while the surrounding transaction sees none of it.
    savepoint = session.begin_nested()

    def write_artifact(content: str) -> str | None:
        if artifacts is None or dry_run or not content:
            return None
        digest, _path = artifacts.put("legacy", content)
        counters.artifacts_written += 1
        imported_artifacts.add(digest)
        return digest

    for row in rows["papers"]:
        doi = (row["doi"] or "").strip()
        if not doi:
            counters.skip("papers", "", "missing_doi")
            continue
        if _marker(session, fingerprint, "papers", doi) is not None:
            counters.count("papers", "duplicates")
            continue
        record = _parse_json(row["record"])
        if not isinstance(record, dict):
            counters.skip("papers", doi, "record_not_json_object")
            continue
        source_id = (doi if doi.lower().startswith("doi:") else f"doi:{doi}")[:180]
        details: dict[str, Any] = {
            "legacy_table": "papers",
            "legacy_key": doi,
            "legacy_updated_at": row["updated_at"],
            "record": record,
        }
        content = row["content"] or ""
        if content:
            details["content_sha256"] = _hash(content)
            digest = write_artifact(content)
            if digest:
                details["content_artifact"] = digest
        source_type = str(record.get("source_type") or "journal")[:40]
        existing = session.get(SourceRecord, source_id)
        if existing is None:
            session.add(
                SourceRecord(
                    id=source_id,
                    url=str(record.get("url") or f"https://doi.org/{doi}")[:2_000],
                    title=str(record.get("title") or doi)[:1_000],
                    source_type=source_type if source_type in SOURCE_TYPES else "journal",
                    metadata_json={"legacy_import": details},
                )
            )
        else:
            existing.metadata_json = {**(existing.metadata_json or {}), "legacy_import": details}
        if not dry_run:
            session.add(
                LegacyImportRecord(
                    source_fingerprint=fingerprint,
                    source_key=source_key,
                    table_name="papers",
                    record_key=doi,
                    target_type="sources",
                    target_id=source_id,
                )
            )
        counters.count("papers", "imported")

    for row in rows["blog_posts"]:
        post_id = (row["post_id"] or "").strip()
        language = (row["language"] or "").strip()[:10]
        key = f"{post_id}\x1f{language}"
        title = (row["title"] or "").strip()
        if not post_id:
            counters.skip("blog_posts", key, "missing_post_id")
            continue
        if not title:
            counters.skip("blog_posts", key, "missing_title")
            continue
        if _marker(session, fingerprint, "blog_posts", key) is not None:
            counters.count("blog_posts", "duplicates")
            continue
        record = _parse_json(row["record"])
        canonical = dict(record) if isinstance(record, dict) else {}
        content = row["content"] or ""
        canonical.update({"id": post_id, "title": title, "content": content, "legacy_import": True})
        if row["doi"]:
            canonical["doi"] = row["doi"]
        existing = session.scalar(
            select(BlogHistory).where(
                BlogHistory.blog_id == blog_id,
                BlogHistory.remote_id == post_id[:300],
                BlogHistory.language == language,
            )
        )
        if existing is None:
            session.add(
                BlogHistory(
                    blog_id=blog_id,
                    remote_id=post_id[:300],
                    language=language,
                    title=title[:1_000],
                    content_hash=_hash(content) if content else None,
                    canonical_record=canonical,
                    snapshot_id=snapshot_id,
                )
            )
        else:
            existing.title = title[:1_000]
            existing.canonical_record = canonical
            existing.content_hash = _hash(content) if content else None
            existing.snapshot_id = snapshot_id
        if not dry_run:
            session.add(
                LegacyImportRecord(
                    source_fingerprint=fingerprint,
                    source_key=source_key,
                    table_name="blog_posts",
                    record_key=key,
                    target_type="blog_history",
                    target_id=f"{post_id}:{language}",
                )
            )
        counters.count("blog_posts", "imported")

    for row in rows["publications"]:
        publication_key = (row["publication_key"] or "").strip()
        if not publication_key:
            counters.skip("publications", "", "missing_publication_key")
            continue
        if _marker(session, fingerprint, "publications", publication_key) is not None:
            counters.count("publications", "duplicates")
            continue
        payload = _parse_json(row["payload"])
        response = _parse_json(row["response"])
        payload = payload if isinstance(payload, dict) else {}
        response = response if isinstance(response, dict) else {}
        pair_id = f"legacy-{_hash(publication_key)[:32]}"
        unit_key = "legacy:publication"
        locale = str(payload.get("language") or payload.get("locale") or "")[:10]
        remote_status = str(row["status"] or "unknown")[:40]
        metadata = {
            "legacy_import": {
                "table": "publications",
                "key": publication_key,
                "slot": row["slot"],
                "updated_at": row["updated_at"],
                "payload": payload,
                "response": response,
            }
        }
        existing = session.scalar(
            select(ContentLedger).where(
                ContentLedger.blog_id == blog_id,
                ContentLedger.pair_id == pair_id,
                ContentLedger.unit_key == unit_key,
            )
        )
        if existing is None:
            ledger = ContentLedger(
                blog_id=blog_id,
                pair_id=pair_id,
                topic_fingerprint=_hash(publication_key),
                locale=locale,
                unit_key=unit_key,
                local_state="legacy_publication",
                remote_id=str(response.get("id") or "")[:300] or None,
                remote_status=remote_status,
                metadata_json=metadata,
            )
            session.add(ledger)
            session.flush()
        else:
            ledger = existing
            ledger.metadata_json = {**(existing.metadata_json or {}), **metadata}
            ledger.remote_status = remote_status
            ledger.local_state = "legacy_publication"
        session.add(
            AuditEvent(
                action="legacy_publication_reconciliation_required",
                resource_type="content_ledger",
                resource_id=ledger.id,
                details={"publication_key": publication_key, "slot": row["slot"], "remote_status": remote_status, "blog_id": blog_id},
            )
        )
        reconciliation.append(ledger.id)
        if not dry_run:
            session.add(
                LegacyImportRecord(
                    source_fingerprint=fingerprint,
                    source_key=source_key,
                    table_name="publications",
                    record_key=publication_key,
                    target_type="content_ledger",
                    target_id=ledger.id,
                )
            )
        counters.count("publications", "imported")

    for row in rows["jobs"]:
        slot = (row["slot"] or "").strip()
        if not slot:
            counters.skip("jobs", "", "missing_slot")
            continue
        if _marker(session, fingerprint, "jobs", slot) is not None:
            counters.count("jobs", "duplicates")
            continue
        session.add(
            AuditEvent(
                action="legacy_job",
                resource_type="legacy_slot",
                resource_id=slot[:120],
                details={
                    "status": row["status"],
                    "attempts": row["attempts"],
                    "retry_at": row["retry_at"],
                    "error": row["error"],
                    "memory": _parse_json(row["memory"]) or {},
                    "updated_at": row["updated_at"],
                    "note": "Legacy job has no run identity; import as audit evidence only.",
                },
            )
        )
        if not dry_run:
            session.add(
                LegacyImportRecord(
                    source_fingerprint=fingerprint,
                    source_key=source_key,
                    table_name="jobs",
                    record_key=slot,
                    target_type="audit_event",
                    target_id="legacy_job",
                )
            )
        counters.count("jobs", "imported")

    for row in rows["drafts"]:
        slot = (row["slot"] or "").strip()
        if not slot:
            counters.skip("drafts", "", "missing_slot")
            continue
        if _marker(session, fingerprint, "drafts", slot) is not None:
            counters.count("drafts", "duplicates")
            continue
        bundle = row["bundle"] or ""
        review = _parse_json(row["review"]) or {}
        digest = write_artifact(bundle)
        session.add(
            AuditEvent(
                action="legacy_draft",
                resource_type="legacy_slot",
                resource_id=slot[:120],
                details={
                    "bundle_sha256": _hash(bundle) if bundle else None,
                    "bundle_artifact": digest,
                    "review": review,
                    "updated_at": row["updated_at"],
                    "note": "Legacy draft bundle is preserved as an artifact; it has no pair or run identity to promote.",
                },
            )
        )
        if not dry_run:
            session.add(
                LegacyImportRecord(
                    source_fingerprint=fingerprint,
                    source_key=source_key,
                    table_name="drafts",
                    record_key=slot,
                    target_type="audit_event",
                    target_id="legacy_draft",
                )
            )
        counters.count("drafts", "imported")

    for row in rows["agent_events"]:
        slot = (row["slot"] or "").strip()
        key = f"{row['id']}:{slot}"
        if _marker(session, fingerprint, "agent_events", key) is not None:
            counters.count("agent_events", "duplicates")
            continue
        session.add(
            AuditEvent(
                action="legacy_agent_event",
                resource_type="legacy_slot",
                resource_id=slot[:120] or f"event:{row['id']}",
                details={"event": row["event"], "details": _parse_json(row["details"]) or {}, "created_at": row["created_at"]},
            )
        )
        if not dry_run:
            session.add(
                LegacyImportRecord(
                    source_fingerprint=fingerprint,
                    source_key=source_key,
                    table_name="agent_events",
                    record_key=key,
                    target_type="audit_event",
                    target_id=f"event:{row['id']}",
                )
            )
        counters.count("agent_events", "imported")

    if dry_run:
        savepoint.rollback()
    else:
        savepoint.commit()

    verification = {
        "sources_total": session.scalar(select(func.count()).select_from(SourceRecord)) or 0,
        "blog_history_for_blog": session.scalar(
            select(func.count()).select_from(BlogHistory).where(BlogHistory.blog_id == blog_id)
        ) or 0,
        "content_ledger_for_blog": session.scalar(
            select(func.count()).select_from(ContentLedger).where(ContentLedger.blog_id == blog_id)
        ) or 0,
        "blog_history_without_profile": session.scalar(
            select(func.count()).select_from(BlogHistory).where(~BlogHistory.blog_id.in_(select(BlogProfile.id)))
        ) or 0,
        "content_ledger_without_profile": session.scalar(
            select(func.count()).select_from(ContentLedger).where(~ContentLedger.blog_id.in_(select(BlogProfile.id)))
        ) or 0,
        "artifacts_readable": 0,
        "artifacts_unreadable": [],
    }
    if artifacts is not None:
        for digest in sorted(imported_artifacts):
            try:
                artifacts.read(digest, kind="legacy")
                verification["artifacts_readable"] += 1
            except Exception:
                verification["artifacts_unreadable"].append(digest)

    report: dict[str, Any] = {
        "dry_run": dry_run,
        "source": {
            "path": str(path),
            "file_sha256": file_digest,
            "fingerprint": fingerprint,
            "blog_id": blog_id,
            "imported_at": imported_at.isoformat(),
        },
        "counts": counters.as_dict(),
        "skipped": counters.skipped,
        "reconciliation_required": {"content_ledger_ids": reconciliation},
        "artifacts_written": counters.artifacts_written,
        "verification": verification,
        "schedule_disabled": not blog.schedule_enabled,
    }
    if not dry_run:
        event = AuditEvent(
            action="legacy_import_report",
            resource_type="blog_profile",
            resource_id=blog_id,
            details=report,
        )
        session.add(event)
        session.flush()
        report["audit_event_id"] = event.id
    return report
