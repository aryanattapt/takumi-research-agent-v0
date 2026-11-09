"""Consistent backups, verified restores, and recovery reports (plan §18.3).

A backup is one directory: a logical database snapshot, a copy of every artifact
that the database references, and a manifest with a hash for each file. A restore
refuses to run on a backup that does not verify, then forces the deployment back
into recovery mode: stale leases are invalidated, in-flight delivery intents are
marked `unknown` (never retried automatically), and a report lists everything an
operator still has to prove before profiles become active again.

The master key is referenced by name only. It is never written into a backup.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import delete, select, text
from sqlalchemy.orm import Session

from takumi_research_agent.persistence.models import (
    AuditEvent,
    Base,
    BlogProfile,
    ContentLedger,
    DeliveryAttempt,
    DeliveryIntent,
    EvidenceSpanRecord,
    GlobalSettings,
    ProviderProfile,
    Run,
    RunEvent,
)
from takumi_research_agent.research.artifacts import ArtifactIntegrityError, ArtifactStore
from takumi_research_agent.security import SecretCipher

BACKUP_FORMAT = 1
MANIFEST_NAME = "manifest.json"
DATABASE_NAME = "database.json"
ARTIFACT_DIR = "artifacts"
MASTER_KEY_REFERENCE = "TAKUMI_MASTER_KEY"
BUSY_RUN_STATUSES = ("running", "delivering", "reconciling")


class BackupBlocked(RuntimeError):
    """An expected fail-closed backup/restore condition."""

    def __init__(self, code: str, detail: str = ""):
        super().__init__(code)
        self.code = code
        self.detail = detail


def _now(now: datetime | None = None) -> datetime:
    value = now or datetime.now(timezone.utc)
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _encode(value: Any) -> Any:
    if isinstance(value, datetime):
        return {"type": "datetime", "value": value.isoformat()}
    if isinstance(value, date):
        return {"type": "date", "value": value.isoformat()}
    if isinstance(value, (bytes, bytearray)):
        return {"type": "bytes", "value": base64.b64encode(bytes(value)).decode("ascii")}
    if isinstance(value, dict):
        return {key: _encode(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_encode(item) for item in value]
    return value


def _decode(value: Any) -> Any:
    if isinstance(value, dict):
        kind = value.get("type")
        if kind == "datetime" and set(value) == {"type", "value"}:
            return datetime.fromisoformat(value["value"])
        if kind == "date" and set(value) == {"type", "value"}:
            return date.fromisoformat(value["value"])
        if kind == "bytes" and set(value) == {"type", "value"}:
            return base64.b64decode(value["value"])
        return {key: _decode(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_decode(item) for item in value]
    return value


def _schema_revision(session: Session) -> str:
    try:
        value = session.execute(text("select version_num from alembic_version")).scalar()
    except Exception:  # the table is absent before the first migration, e.g. create_all test databases
        value = None
    return str(value) if value else "unmanaged"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=".tmp-", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _split_reference(artifacts_root: Path, path_text: str) -> tuple[str, str]:
    """Return (kind, digest) from the content-addressed path stored on the evidence row."""
    root = artifacts_root.resolve()
    path = Path(path_text)
    if not path.is_absolute():
        path = Path.cwd() / path
    try:
        relative = path.resolve().relative_to(root)
    except ValueError as exc:
        raise BackupBlocked("artifact_path_outside_root", path_text) from exc
    parts = relative.parts
    if len(parts) != 3 or len(parts[2]) != 64:
        raise BackupBlocked("artifact_path_invalid", path_text)
    return parts[0], parts[2]


def _referenced_artifacts(session: Session, artifacts_root: Path) -> dict[tuple[str, str], str]:
    """Map (kind, digest) to the evidence id that references it; artifacts are deduplicated."""
    references: dict[tuple[str, str], str] = {}
    for row in session.scalars(select(EvidenceSpanRecord).order_by(EvidenceSpanRecord.id)):
        if not row.artifact_path:
            raise BackupBlocked("evidence_artifact_missing", row.id)
        kind, digest = _split_reference(artifacts_root, row.artifact_path)
        if digest != row.content_hash:
            raise BackupBlocked("evidence_artifact_reference_mismatch", row.id)
        references.setdefault((kind, digest), row.id)
    return references


def create_backup(
    sessions,
    *,
    artifacts_root: Path,
    output_dir: Path,
    now: datetime | None = None,
    backup_id: str | None = None,
) -> dict[str, Any]:
    """Snapshot the database, copy every referenced artifact, then seal a manifest."""
    artifacts_root = artifacts_root.resolve()
    output_dir = output_dir.resolve()
    if output_dir == artifacts_root or artifacts_root in output_dir.parents:
        raise BackupBlocked("backup_inside_artifact_root", str(output_dir))
    created_at = _now(now)
    identifier = backup_id or created_at.strftime("%Y%m%dT%H%M%SZ")
    backup_dir = output_dir / identifier
    if backup_dir.exists() and any(backup_dir.iterdir()):
        raise BackupBlocked("backup_target_exists", str(backup_dir))
    backup_dir.mkdir(parents=True, exist_ok=True)
    store = ArtifactStore(artifacts_root)

    with sessions() as session:
        # One read transaction is the consistency boundary for the logical snapshot.
        tables: dict[str, list[dict[str, Any]]] = {}
        for table in Base.metadata.sorted_tables:
            rows = [dict(row._mapping) for row in session.execute(select(table))]
            tables[table.name] = [{key: _encode(value) for key, value in row.items()} for row in rows]
        schema_revision = _schema_revision(session)
        references = _referenced_artifacts(session, artifacts_root)

    artifact_entries: list[dict[str, Any]] = []
    for (kind, digest), evidence_id in sorted(references.items()):
        try:
            data = store.read(digest, kind=kind)
        except (ArtifactIntegrityError, ValueError) as exc:
            raise BackupBlocked("artifact_integrity_failed", f"{evidence_id}:{kind}/{digest}") from exc
        target = backup_dir / ARTIFACT_DIR / kind / digest
        _write_atomic(target, data)
        artifact_entries.append(
            {"kind": kind, "digest": digest, "size": len(data), "sha256": hashlib.sha256(data).hexdigest(), "evidence_id": evidence_id}
        )

    database_payload = json.dumps({"format": BACKUP_FORMAT, "schema_revision": schema_revision, "tables": tables}, sort_keys=True).encode("utf-8")
    _write_atomic(backup_dir / DATABASE_NAME, database_payload)
    manifest = {
        "format": BACKUP_FORMAT,
        "backup_id": identifier,
        "created_at": created_at.isoformat(),
        "schema_revision": schema_revision,
        "key_reference": MASTER_KEY_REFERENCE,
        "database": {
            "file": DATABASE_NAME,
            "sha256": hashlib.sha256(database_payload).hexdigest(),
            "size": len(database_payload),
            "rows": {name: len(rows) for name, rows in tables.items()},
        },
        "artifacts": artifact_entries,
    }
    _write_atomic(backup_dir / MANIFEST_NAME, json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8"))
    return manifest


def _load_manifest(backup_path: Path) -> tuple[Path, dict[str, Any]]:
    backup_dir = Path(backup_path)
    if backup_dir.is_file():
        if backup_dir.name != MANIFEST_NAME:
            raise BackupBlocked("backup_path_invalid", str(backup_dir))
        backup_dir = backup_dir.parent
    manifest_path = backup_dir / MANIFEST_NAME
    if not manifest_path.is_file():
        raise BackupBlocked("backup_manifest_missing", str(manifest_path))
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BackupBlocked("backup_manifest_unreadable", str(manifest_path)) from exc
    return backup_dir, manifest


def verify_backup(backup_path: Path) -> dict[str, Any]:
    """Re-hash every file in the backup; a single mismatch means it cannot be restored."""
    backup_dir, manifest = _load_manifest(backup_path)
    problems: list[str] = []
    if manifest.get("format") != BACKUP_FORMAT:
        problems.append("unsupported_format")
    database = manifest.get("database") or {}
    database_file = backup_dir / str(database.get("file", DATABASE_NAME))
    tables: dict[str, Any] = {}
    if not database_file.is_file():
        problems.append("database_file_missing")
    else:
        if _sha256_file(database_file) != database.get("sha256"):
            problems.append("database_file_hash_mismatch")
        try:
            payload = json.loads(database_file.read_text(encoding="utf-8"))
            tables = payload.get("tables") or {}
            if payload.get("schema_revision") != manifest.get("schema_revision"):
                problems.append("database_schema_revision_mismatch")
            for name, expected in (database.get("rows") or {}).items():
                if len(tables.get(name) or []) != expected:
                    problems.append(f"row_count_mismatch:{name}")
        except (OSError, json.JSONDecodeError):
            problems.append("database_file_unreadable")
    for entry in manifest.get("artifacts") or []:
        target = backup_dir / ARTIFACT_DIR / str(entry.get("kind")) / str(entry.get("digest"))
        if not target.is_file():
            problems.append(f"artifact_missing:{entry.get('digest')}")
            continue
        if _sha256_file(target) != entry.get("sha256"):
            problems.append(f"artifact_hash_mismatch:{entry.get('digest')}")
    return {
        "ok": not problems,
        "problems": problems,
        "backup_dir": str(backup_dir),
        "backup_id": manifest.get("backup_id"),
        "schema_revision": manifest.get("schema_revision"),
        "tables": {name: len(rows) for name, rows in tables.items()},
        "artifacts": len(manifest.get("artifacts") or []),
    }


def artifact_integrity_report(session: Session, *, artifacts_root: Path, limit: int = 500) -> dict[str, Any]:
    """Check every referenced artifact still matches its hash; used by recovery and the API."""
    store = ArtifactStore(artifacts_root.resolve())
    total = 0
    verified = 0
    missing: list[str] = []
    corrupt: list[str] = []
    for row in session.scalars(select(EvidenceSpanRecord).order_by(EvidenceSpanRecord.id).limit(limit)):
        total += 1
        try:
            kind, digest = _split_reference(artifacts_root, row.artifact_path)
        except BackupBlocked:
            corrupt.append(row.id)
            continue
        if digest != row.content_hash:
            corrupt.append(row.id)
            continue
        try:
            store.read(digest, kind=kind)
            verified += 1
        except ArtifactIntegrityError as exc:
            (missing if "missing" in str(exc) else corrupt).append(row.id)
        except ValueError:
            corrupt.append(row.id)
    return {"checked": True, "total": total, "verified": verified, "missing": missing, "corrupt": corrupt, "truncated": total >= limit}


def _decrypt_failures(session: Session, master_key: str) -> tuple[list[str], list[str], bool]:
    present = bool(master_key) and len(master_key) >= 32
    if not present:
        return [], [], False
    cipher = SecretCipher(master_key)
    providers: list[str] = []
    blogs: list[str] = []
    for provider in session.scalars(select(ProviderProfile)):
        if not provider.encrypted_api_key:
            continue
        try:
            cipher.decrypt(provider.encrypted_api_key)
        except ValueError:
            providers.append(provider.id)
    for blog in session.scalars(select(BlogProfile)):
        if not blog.encrypted_api_token:
            continue
        try:
            cipher.decrypt(blog.encrypted_api_token)
        except ValueError:
            blogs.append(blog.id)
    return providers, blogs, True


def restore_backup(
    session: Session,
    *,
    backup_path: Path,
    artifacts_root: Path,
    master_key: str = "",
    acknowledge: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Replace local data with a verified backup and force recovery mode before anyone writes again."""
    if not acknowledge:
        raise BackupBlocked("restore_requires_acknowledgement", "pass --acknowledge to replace local data")
    verified = verify_backup(backup_path)
    if not verified["ok"]:
        raise BackupBlocked("backup_verification_failed", ",".join(verified["problems"][:5]))
    backup_dir, manifest = _load_manifest(backup_path)
    payload = json.loads((backup_dir / str(manifest["database"]["file"])).read_text(encoding="utf-8"))
    tables = payload.get("tables") or {}
    restored_at = _now(now)
    current_revision = _schema_revision(session)
    if manifest.get("schema_revision") != current_revision and current_revision != "unmanaged":
        raise BackupBlocked("schema_revision_mismatch", f"backup={manifest.get('schema_revision')} current={current_revision}")

    for table in reversed(Base.metadata.sorted_tables):
        session.execute(delete(table))
    for table in Base.metadata.sorted_tables:
        rows = [{key: _decode(value) for key, value in row.items()} for row in tables.get(table.name) or []]
        if rows:
            session.execute(table.insert(), rows)
    session.flush()
    session.expunge_all()

    control = session.get(GlobalSettings, 1)
    if control is None:
        control = GlobalSettings(id=1)
        session.add(control)
        session.flush()
    control.recovery_mode = True
    control.content_mutations_paused = True
    control.revision += 1

    blocked_runs: list[str] = []
    for run in session.scalars(select(Run).where(Run.status.in_(BUSY_RUN_STATUSES))).all():
        run.status = "blocked"
        run.stage = "restore_recovery"
        run.pipeline_outcome = "restore_fault"
        run.lease_owner = None
        run.lease_until = None
        run.lease_epoch += 1
        blocked_runs.append(run.id)
        session.add(RunEvent(run_id=run.id, event_type="restore_lease_invalidated", details={"restored_from": manifest.get("backup_id")}))

    unknown_intents: list[str] = []
    for intent in session.scalars(select(DeliveryIntent).where(DeliveryIntent.state == "dispatching")).all():
        # The remote outcome of an in-flight POST cannot be reconstructed from the backup.
        intent.state = "unknown"
        intent.response_summary = {"error": "restore_interrupted_dispatch", "reconciliation_required": True}
        unknown_intents.append(intent.id)
        for attempt in session.scalars(select(DeliveryAttempt).where(DeliveryAttempt.intent_id == intent.id, DeliveryAttempt.outcome == "in_flight")):
            attempt.outcome = "unknown"
            attempt.finished_at = restored_at
        session.add(
            RunEvent(
                run_id=intent.run_id,
                event_type="delivery_unknown",
                details={"intent_id": intent.id, "unit_key": intent.unit_key, "error": "restore_interrupted_dispatch"},
            )
        )
        ledger = session.scalar(
            select(ContentLedger).where(
                ContentLedger.blog_id == intent.blog_id,
                ContentLedger.pair_id == intent.pair_id,
                ContentLedger.unit_key == intent.unit_key,
            )
        )
        if ledger is not None:
            ledger.local_state = "delivery_unknown"
            ledger.remote_status = "unknown"

    integrity = artifact_integrity_report(session, artifacts_root=artifacts_root)
    provider_failures, blog_failures, key_present = _decrypt_failures(session, master_key)
    revalidation = [blog.id for blog in session.scalars(select(BlogProfile)) if blog.validated_revision != blog.revision]
    schedule_review = [
        blog.id for blog in session.scalars(select(BlogProfile)) if blog.active and blog.schedule_enabled
    ]
    report = {
        "backup_id": manifest.get("backup_id"),
        "backup_created_at": manifest.get("created_at"),
        "restored_at": restored_at.isoformat(),
        "schema_revision": {"backup": manifest.get("schema_revision"), "current": current_revision},
        "tables": {name: len(tables.get(name) or []) for name in tables},
        "artifacts": integrity,
        "master_key_present": key_present,
        "blocked_runs": blocked_runs,
        "unknown_intents": unknown_intents,
        "profiles_requiring_revalidation": revalidation,
        "providers_requiring_recredential": provider_failures,
        "blogs_requiring_recredential": blog_failures,
        "schedule_cancel_or_resync_review": schedule_review,
        "recovery_mode": True,
    }
    session.add(
        AuditEvent(
            action="system_restore",
            resource_type="backup",
            resource_id=str(manifest.get("backup_id")),
            details=report,
        )
    )
    session.flush()
    return report


def last_restore_report(session: Session) -> dict[str, Any] | None:
    event = session.scalar(select(AuditEvent).where(AuditEvent.action == "system_restore").order_by(AuditEvent.created_at.desc()).limit(1))
    if event is None:
        return None
    return {"backup_id": event.resource_id, "created_at": event.created_at.isoformat(), **(event.details or {})}
