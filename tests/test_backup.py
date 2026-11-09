from __future__ import annotations

import json
from datetime import date, datetime, timezone

import pytest
from sqlalchemy import select, text

from takumi_research_agent.operations.backup import (
    BackupBlocked,
    artifact_integrity_report,
    create_backup,
    last_restore_report,
    restore_backup,
    verify_backup,
)
from takumi_research_agent.persistence.database import create_database
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
    SourceRecord,
)
from takumi_research_agent.research.artifacts import ArtifactStore
from takumi_research_agent.security import SecretCipher

MASTER_KEY = "test-master-key-" * 4


def _database():
    engine, sessions = create_database("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return engine, sessions


def _seed(sessions, artifacts_root, *, key: str = MASTER_KEY) -> dict[str, str]:
    cipher = SecretCipher(key)
    store = ArtifactStore(artifacts_root)
    digest, path = store.put("source", "Measured solar-cell efficiency improved by 2.1% in the trial.")
    with sessions.begin() as session:
        session.add(GlobalSettings(id=1, recovery_mode=False, content_mutations_paused=False))
        provider = ProviderProfile(
            name="Model",
            provider_type="openai_compatible",
            base_url="https://model.example/v1",
            model="test",
            encrypted_api_key=cipher.encrypt("provider-secret"),
            capability_result={"status": "reachable"},
        )
        session.add(provider)
        session.flush()
        blog = BlogProfile(
            name="Blog",
            base_url="https://blog.example/api",
            provider_id=provider.id,
            encrypted_api_token=cipher.encrypt("blog-secret"),
            capability_result={"list_posts": True, "history_complete": True},
            validated_revision=1,
            active=True,
            schedule_enabled=True,
        )
        session.add(blog)
        session.flush()
        run = Run(
            blog_id=blog.id,
            editorial_date=date(2026, 11, 6),
            quota_slot_key="slot-1",
            target_at=datetime(2026, 11, 6, 6, 0, tzinfo=timezone.utc),
            status="running",
            stage="researching",
            execution_mode="sandbox",
            config_snapshot={"blog_revision": 1},
            lease_owner="worker-a",
            lease_until=datetime(2026, 11, 6, 5, 0, tzinfo=timezone.utc),
            lease_epoch=3,
            next_run_at=datetime(2026, 11, 6, 4, 0, tzinfo=timezone.utc),
        )
        session.add(run)
        session.flush()
        session.add(SourceRecord(id="doi:10.1/x", url="https://doi.org/10.1/x", title="Trial", source_type="journal", metadata_json={}))
        session.add(
            EvidenceSpanRecord(
                id="doi:10.1/x:span-1",
                source_id="doi:10.1/x",
                content_hash=digest,
                quote="Measured solar-cell efficiency improved by 2.1% in the trial.",
                locator="p.1",
                artifact_path=str(path),
            )
        )
        intent = DeliveryIntent(
            run_id=run.id,
            blog_id=blog.id,
            pair_id="pair-1",
            unit_key="article:id",
            action="create_draft",
            operation_id="createPost",
            destination_epoch=1,
            delivery_key="delivery-key-1",
            approval_fingerprint="fingerprint-1",
            payload={"title": "t"},
            state="dispatching",
        )
        session.add(intent)
        session.flush()
        session.add(DeliveryAttempt(intent_id=intent.id, attempt_number=1, outcome="in_flight"))
        session.add(
            ContentLedger(
                blog_id=blog.id,
                pair_id="pair-1",
                topic_fingerprint="topic-1",
                locale="id",
                unit_key="article:id",
                local_state="delivery_dispatching",
                remote_status="unknown",
            )
        )
    return {"provider_id": provider.id, "blog_id": blog.id, "run_id": run.id, "intent_id": intent.id, "digest": digest}


def test_backup_seals_verified_artifacts_and_never_stores_the_master_key(tmp_path):
    engine, sessions = _database()
    artifacts_root = tmp_path / "artifacts"
    seeded = _seed(sessions, artifacts_root)
    manifest = create_backup(sessions, artifacts_root=artifacts_root, output_dir=tmp_path / "backups")
    assert manifest["key_reference"] == "TAKUMI_MASTER_KEY"
    assert manifest["database"]["rows"]["runs"] == 1
    assert manifest["database"]["rows"]["evidence_spans"] == 1
    assert len(manifest["artifacts"]) == 1
    backup_dir = tmp_path / "backups" / manifest["backup_id"]
    assert (backup_dir / "artifacts" / "source" / seeded["digest"]).is_file()
    stored = (backup_dir / "manifest.json").read_text(encoding="utf-8") + (backup_dir / "database.json").read_text(encoding="utf-8")
    assert MASTER_KEY not in stored
    assert "provider-secret" not in stored and "blog-secret" not in stored
    assert verify_backup(backup_dir)["ok"] is True
    engine.dispose()


def test_backup_fails_closed_when_referenced_artifact_bytes_changed(tmp_path):
    engine, sessions = _database()
    artifacts_root = tmp_path / "artifacts"
    seeded = _seed(sessions, artifacts_root)
    target = artifacts_root / "source" / seeded["digest"][:2] / seeded["digest"]
    target.write_bytes(b"tampered content")
    with pytest.raises(BackupBlocked) as error:
        create_backup(sessions, artifacts_root=artifacts_root, output_dir=tmp_path / "backups")
    assert error.value.code == "artifact_integrity_failed"
    engine.dispose()


def test_verify_backup_detects_tampered_database_and_artifact_copies(tmp_path):
    engine, sessions = _database()
    artifacts_root = tmp_path / "artifacts"
    seeded = _seed(sessions, artifacts_root)
    manifest = create_backup(sessions, artifacts_root=artifacts_root, output_dir=tmp_path / "backups")
    backup_dir = tmp_path / "backups" / manifest["backup_id"]
    (backup_dir / "artifacts" / "source" / seeded["digest"]).write_bytes(b"other bytes")
    (backup_dir / "database.json").write_text("{}", encoding="utf-8")
    result = verify_backup(backup_dir)
    assert result["ok"] is False
    assert "database_file_hash_mismatch" in result["problems"]
    assert f"artifact_hash_mismatch:{seeded['digest']}" in result["problems"]
    engine.dispose()


def test_restore_requires_acknowledgement_and_a_verifying_backup(tmp_path):
    engine, sessions = _database()
    artifacts_root = tmp_path / "artifacts"
    seeded = _seed(sessions, artifacts_root)
    manifest = create_backup(sessions, artifacts_root=artifacts_root, output_dir=tmp_path / "backups")
    backup_dir = tmp_path / "backups" / manifest["backup_id"]
    with sessions.begin() as session, pytest.raises(BackupBlocked) as error:
        restore_backup(session, backup_path=backup_dir, artifacts_root=artifacts_root, acknowledge=False)
    assert error.value.code == "restore_requires_acknowledgement"
    (backup_dir / "artifacts" / "source" / seeded["digest"]).write_bytes(b"corrupt")
    with sessions.begin() as session, pytest.raises(BackupBlocked) as error:
        restore_backup(session, backup_path=backup_dir, artifacts_root=artifacts_root, master_key=MASTER_KEY, acknowledge=True)
    assert error.value.code == "backup_verification_failed"
    engine.dispose()


def test_restore_rebuilds_data_and_forces_recovery_mode(tmp_path):
    engine, sessions = _database()
    artifacts_root = tmp_path / "artifacts"
    seeded = _seed(sessions, artifacts_root)
    manifest = create_backup(sessions, artifacts_root=artifacts_root, output_dir=tmp_path / "backups")
    backup_dir = tmp_path / "backups" / manifest["backup_id"]
    with sessions.begin() as session:
        session.add(BlogProfile(name="Added after backup", base_url="https://later.example/api"))
    with sessions.begin() as session:
        report = restore_backup(
            session,
            backup_path=backup_dir,
            artifacts_root=artifacts_root,
            master_key=MASTER_KEY,
            acknowledge=True,
        )
    with sessions.begin() as session:
        names = session.scalars(select(BlogProfile.name)).all()
        assert names == ["Blog"]
        control = session.get(GlobalSettings, 1)
        assert control.recovery_mode is True
        assert control.content_mutations_paused is True
        run = session.get(Run, seeded["run_id"])
        assert run.status == "blocked"
        assert run.stage == "restore_recovery"
        assert run.pipeline_outcome == "restore_fault"
        assert run.lease_owner is None and run.lease_until is None
        assert run.lease_epoch == 4
        intent = session.get(DeliveryIntent, seeded["intent_id"])
        assert intent.state == "unknown"
        assert intent.response_summary["reconciliation_required"] is True
        attempt = session.scalar(select(DeliveryAttempt).where(DeliveryAttempt.intent_id == intent.id))
        assert attempt.outcome == "unknown"
        ledger = session.scalar(select(ContentLedger).where(ContentLedger.pair_id == "pair-1"))
        assert ledger.remote_status == "unknown"
        assert report["blocked_runs"] == [seeded["run_id"]]
        assert report["unknown_intents"] == [seeded["intent_id"]]
        assert report["artifacts"]["verified"] == 1
        assert report["master_key_present"] is True
        assert session.scalar(select(RunEvent).where(RunEvent.event_type == "restore_lease_invalidated")) is not None
        assert session.scalar(select(AuditEvent).where(AuditEvent.action == "system_restore")) is not None
        stored = last_restore_report(session)
        assert stored is not None and stored["backup_id"] == manifest["backup_id"]
        assert stored["schema_revision"]["backup"] == "unmanaged"
    engine.dispose()


def test_restore_report_flags_revalidation_and_unreadable_credentials(tmp_path):
    engine, sessions = _database()
    artifacts_root = tmp_path / "artifacts"
    seeded = _seed(sessions, artifacts_root)
    with sessions.begin() as session:
        blog = session.get(BlogProfile, seeded["blog_id"])
        blog.revision = 2
    manifest = create_backup(sessions, artifacts_root=artifacts_root, output_dir=tmp_path / "backups")
    backup_dir = tmp_path / "backups" / manifest["backup_id"]
    with sessions.begin() as session:
        report = restore_backup(
            session,
            backup_path=backup_dir,
            artifacts_root=artifacts_root,
            master_key="different-master-key-" * 2,
            acknowledge=True,
        )
    assert report["profiles_requiring_revalidation"] == [seeded["blog_id"]]
    assert report["providers_requiring_recredential"] == [seeded["provider_id"]]
    assert report["blogs_requiring_recredential"] == [seeded["blog_id"]]
    assert report["schedule_cancel_or_resync_review"] == [seeded["blog_id"]]
    engine.dispose()


def test_restore_without_master_key_reports_it_missing_instead_of_claiming_success(tmp_path):
    engine, sessions = _database()
    artifacts_root = tmp_path / "artifacts"
    _seed(sessions, artifacts_root)
    manifest = create_backup(sessions, artifacts_root=artifacts_root, output_dir=tmp_path / "backups")
    with sessions.begin() as session:
        report = restore_backup(
            session,
            backup_path=tmp_path / "backups" / manifest["backup_id"],
            artifacts_root=artifacts_root,
            master_key="",
            acknowledge=True,
        )
    assert report["master_key_present"] is False
    assert report["providers_requiring_recredential"] == []
    engine.dispose()


def test_restore_blocks_when_schema_revision_differs(tmp_path):
    engine, sessions = _database()
    artifacts_root = tmp_path / "artifacts"
    _seed(sessions, artifacts_root)
    with sessions.begin() as session:
        session.execute(text("create table alembic_version (version_num varchar(32) not null primary key)"))
        session.execute(text("insert into alembic_version values ('rev-a')"))
    manifest = create_backup(sessions, artifacts_root=artifacts_root, output_dir=tmp_path / "backups")
    assert manifest["schema_revision"] == "rev-a"
    with sessions.begin() as session:
        session.execute(text("update alembic_version set version_num = 'rev-b'"))
    with sessions.begin() as session, pytest.raises(BackupBlocked) as error:
        restore_backup(
            session,
            backup_path=tmp_path / "backups" / manifest["backup_id"],
            artifacts_root=artifacts_root,
            master_key=MASTER_KEY,
            acknowledge=True,
        )
    assert error.value.code == "schema_revision_mismatch"
    engine.dispose()


def test_artifact_integrity_report_lists_missing_and_corrupt_evidence(tmp_path):
    engine, sessions = _database()
    artifacts_root = tmp_path / "artifacts"
    store = ArtifactStore(artifacts_root)
    good_digest, good_path = store.put("source", "good")
    lost_digest, lost_path = store.put("source", "lost")
    bad_digest, bad_path = store.put("source", "bad")
    with sessions.begin() as session:
        session.add(SourceRecord(id="s", url="https://doi.org/10.1/x", title="t", source_type="journal", metadata_json={}))
        for index, (digest, path) in enumerate([(good_digest, good_path), (lost_digest, lost_path), (bad_digest, bad_path)]):
            session.add(
                EvidenceSpanRecord(
                    id=f"s:span-{index}",
                    source_id="s",
                    content_hash=digest,
                    quote="q",
                    artifact_path=str(path),
                )
            )
    lost_path.unlink()
    bad_path.write_bytes(b"changed")
    with sessions.begin() as session:
        report = artifact_integrity_report(session, artifacts_root=artifacts_root)
    assert report["checked"] is True
    assert report["total"] == 3
    assert report["verified"] == 1
    assert report["missing"] == ["s:span-1"]
    assert report["corrupt"] == ["s:span-2"]
    engine.dispose()


def test_backup_manifest_json_is_readable(tmp_path):
    engine, sessions = _database()
    artifacts_root = tmp_path / "artifacts"
    _seed(sessions, artifacts_root)
    manifest = create_backup(sessions, artifacts_root=artifacts_root, output_dir=tmp_path / "backups")
    stored = json.loads((tmp_path / "backups" / manifest["backup_id"] / "manifest.json").read_text(encoding="utf-8"))
    assert stored["format"] == 1
    assert stored["artifacts"][0]["size"] > 0
    engine.dispose()
