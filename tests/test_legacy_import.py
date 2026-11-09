from __future__ import annotations

import json
import shutil
import sqlite3
import tempfile
from pathlib import Path

import pytest
from sqlalchemy import select

from takumi_research_agent.operations.backup import create_backup
from takumi_research_agent.operations.legacy_import import LegacyImportBlocked, import_legacy_sqlite
from takumi_research_agent.persistence.database import create_database
from takumi_research_agent.persistence.models import (
    AuditEvent,
    Base,
    BlogHistory,
    BlogProfile,
    ContentLedger,
    LegacyImportRecord,
    SourceRecord,
)
from takumi_research_agent.research.artifacts import ArtifactStore
from takumi_research_agent.store import Store


def _database():
    engine, sessions = create_database("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return engine, sessions


def _legacy_store(tmp_path: Path) -> tuple[Path, Store]:
    """Build a legacy file with the real legacy writer, then import it."""
    data_dir = tmp_path / "legacy"
    store = Store(data_dir)
    store.save_paper(
        "10.1234/abc",
        {"title": "Solar cell trial", "url": "https://doi.org/10.1234/abc", "source_type": "journal"},
        "Full text of the solar cell trial.",
    )
    store.save_paper("10.1234/no-content", {"title": "Second paper"})
    store.replace_blog_posts([
        {"id": "42", "language": "id", "title": "Artikel lama", "content": "Isi artikel", "doi": "10.1234/abc"},
        {"id": "42", "language": "en", "title": "Old article", "content": "Body", "doi": "10.1234/abc"},
        {"id": "43", "language": "", "title": "", "content": "tanpa judul"},
    ])
    store.save_publication("pub-key-1", "2026-11-01", "published", {"language": "id", "title": "Artikel lama"}, {"id": "remote-9"})
    store.set_job("2026-11-01", "completed")
    store.save_draft("2026-11-01", {"articles": {"id": "draft body"}}, {"decision": "draft"})
    store.record_event("2026-11-01", "research_finished", {"sources": 2})
    return store.db_path, store


def _legacy_snapshot(path: Path) -> dict[str, list[list]]:
    """Logical content of the legacy file, independent of SQLite WAL checkpointing."""
    where = Path(tempfile.mkdtemp()) / "takumi.sqlite3"
    for suffix in ("", "-wal", "-shm"):
        source = Path(str(path) + suffix)
        if source.exists():
            shutil.copyfile(source, Path(str(where) + suffix))
    connection = sqlite3.connect(f"file:{where.as_posix()}?mode=ro", uri=True)
    try:
        return {
            table: [list(row) for row in connection.execute(f"select * from {table} order by 1, 2")]
            for table in ("papers", "blog_posts", "publications", "jobs", "drafts", "agent_events")
        }
    finally:
        connection.close()


def _blog(session, *, active: bool = False) -> BlogProfile:
    blog = BlogProfile(name="Legacy blog", base_url="https://blog.example/api", active=active, schedule_enabled=not active)
    session.add(blog)
    session.flush()
    return blog


def test_import_maps_legacy_records_into_one_blog_and_preserves_the_source_file(tmp_path):
    engine, sessions = _database()
    legacy_path, _store = _legacy_store(tmp_path)
    original_rows = _legacy_snapshot(legacy_path)
    artifacts = ArtifactStore(tmp_path / "artifacts")
    with sessions.begin() as session:
        blog = _blog(session)
        report = import_legacy_sqlite(session, database_path=legacy_path, blog_id=blog.id, artifacts=artifacts)
        blog_id = blog.id
    assert report["counts"]["papers"] == {"imported": 2, "duplicates": 0, "skipped": 0}
    assert report["counts"]["blog_posts"] == {"imported": 2, "duplicates": 0, "skipped": 1}
    assert report["counts"]["publications"] == {"imported": 1, "duplicates": 0, "skipped": 0}
    assert report["counts"]["jobs"] == {"imported": 1, "duplicates": 0, "skipped": 0}
    assert report["counts"]["drafts"] == {"imported": 1, "duplicates": 0, "skipped": 0}
    assert report["counts"]["agent_events"] == {"imported": 1, "duplicates": 0, "skipped": 0}
    assert report["skipped"][0]["reason"] == "missing_title"
    assert report["artifacts_written"] == 2  # one paper body + one draft bundle
    # The legacy file is only ever opened read-only; its logical rows must be untouched.
    assert _legacy_snapshot(legacy_path) == original_rows

    with sessions.begin() as session:
        sources = {row.id: row for row in session.scalars(select(SourceRecord))}
        assert set(sources) == {"doi:10.1234/abc", "doi:10.1234/no-content"}
        assert sources["doi:10.1234/abc"].title == "Solar cell trial"
        assert sources["doi:10.1234/abc"].source_type == "journal"
        digest = sources["doi:10.1234/abc"].metadata_json["legacy_import"]["content_artifact"]
        assert artifacts.read(digest, kind="legacy").decode() == "Full text of the solar cell trial."
        history = session.scalars(select(BlogHistory).where(BlogHistory.blog_id == blog_id)).all()
        assert {(row.remote_id, row.language) for row in history} == {("42", "id"), ("42", "en")}
        assert all(row.canonical_record["legacy_import"] is True for row in history)
        ledger = session.scalars(select(ContentLedger).where(ContentLedger.blog_id == blog_id)).one()
        assert ledger.local_state == "legacy_publication"
        assert ledger.remote_status == "published"
        assert ledger.remote_id == "remote-9"
        assert ledger.metadata_json["legacy_import"]["slot"] == "2026-11-01"
        reconciliation = session.scalar(select(AuditEvent).where(AuditEvent.action == "legacy_publication_reconciliation_required"))
        assert reconciliation is not None and reconciliation.resource_id == ledger.id
        reports = session.scalar(select(AuditEvent).where(AuditEvent.action == "legacy_import_report"))
        assert reports is not None and reports.resource_id == blog_id
        assert report["verification"]["blog_history_for_blog"] == 2
        assert report["verification"]["content_ledger_for_blog"] == 1
        assert report["verification"]["artifacts_readable"] == 2
        assert report["verification"]["artifacts_unreadable"] == []
        assert report["verification"]["blog_history_without_profile"] == 0
    engine.dispose()


def test_reimport_is_idempotent_and_writes_no_duplicate_rows(tmp_path):
    engine, sessions = _database()
    legacy_path, _store = _legacy_store(tmp_path)
    artifacts = ArtifactStore(tmp_path / "artifacts")
    with sessions.begin() as session:
        blog = _blog(session)
        first = import_legacy_sqlite(session, database_path=legacy_path, blog_id=blog.id, artifacts=artifacts)
        blog_id = blog.id
    with sessions.begin() as session:
        second = import_legacy_sqlite(session, database_path=legacy_path, blog_id=blog_id, artifacts=artifacts)
    assert first["counts"]["papers"]["imported"] == 2
    assert second["counts"]["papers"] == {"imported": 0, "duplicates": 2, "skipped": 0}
    assert second["counts"]["publications"] == {"imported": 0, "duplicates": 1, "skipped": 0}
    assert second["artifacts_written"] == 0
    with sessions.begin() as session:
        assert len(session.scalars(select(SourceRecord)).all()) == 2
        assert len(session.scalars(select(BlogHistory).where(BlogHistory.blog_id == blog_id)).all()) == 2
        assert len(session.scalars(select(ContentLedger).where(ContentLedger.blog_id == blog_id)).all()) == 1
        assert len(session.scalars(select(LegacyImportRecord)).all()) == 8
    engine.dispose()


def test_dry_run_reports_without_writing(tmp_path):
    engine, sessions = _database()
    legacy_path, _store = _legacy_store(tmp_path)
    artifacts = ArtifactStore(tmp_path / "artifacts")
    with sessions.begin() as session:
        blog = _blog(session)
        report = import_legacy_sqlite(session, database_path=legacy_path, blog_id=blog.id, artifacts=artifacts, dry_run=True)
    assert report["dry_run"] is True
    assert report["counts"]["papers"]["imported"] == 2
    assert "audit_event_id" not in report
    with sessions.begin() as session:
        assert session.scalars(select(SourceRecord)).all() == []
        assert session.scalars(select(LegacyImportRecord)).all() == []
        assert session.scalars(select(AuditEvent)).all() == []
    assert not (tmp_path / "artifacts" / "legacy").exists()
    engine.dispose()


def test_active_profile_is_refused_and_missing_paths_are_reported(tmp_path):
    engine, sessions = _database()
    legacy_path, _store = _legacy_store(tmp_path)
    with sessions.begin() as session:
        active = _blog(session, active=True)
        with pytest.raises(LegacyImportBlocked) as error:
            import_legacy_sqlite(session, database_path=legacy_path, blog_id=active.id)
        assert error.value.code == "blog_profile_must_be_inactive"
        inactive = _blog(session)
        with pytest.raises(LegacyImportBlocked) as error:
            import_legacy_sqlite(session, database_path=tmp_path / "missing.sqlite3", blog_id=inactive.id)
        assert error.value.code == "legacy_database_missing"
        with pytest.raises(LegacyImportBlocked) as error:
            import_legacy_sqlite(session, database_path=legacy_path, blog_id="not-a-blog")
        assert error.value.code == "blog_profile_not_found"
    engine.dispose()


def test_import_runs_after_a_verified_backup_of_the_current_database(tmp_path):
    """The CLI creates this backup first; prove the two features compose."""
    engine, sessions = _database()
    legacy_path, _store = _legacy_store(tmp_path)
    artifacts = ArtifactStore(tmp_path / "artifacts")
    with sessions.begin() as session:
        _blog(session)
    manifest = create_backup(sessions, artifacts_root=tmp_path / "artifacts", output_dir=tmp_path / "backups")
    assert manifest["database"]["rows"]["sources"] == 0
    with sessions.begin() as session:
        blog = session.scalars(select(BlogProfile)).first()
        report = import_legacy_sqlite(session, database_path=legacy_path, blog_id=blog.id, artifacts=artifacts)
    assert report["counts"]["papers"]["imported"] == 2
    assert json.dumps(report["counts"])  # report is JSON-serialisable for the CLI
    engine.dispose()
