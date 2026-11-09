#!/usr/bin/env python3
"""End-to-end smoke test for Takumi data-lifecycle operations.

Runs on a throwaway SQLite database and data directory inside the repository, so it
never touches real data. Covers the CLI surface added by backup/restore and the
legacy SQLite import: backup -> verify -> import -> re-import (idempotent) -> restore.

Usage (from anywhere):
    python .semutcode/skills/jalur/takumi-data-lifecycle-ops/scripts/ops_smoke.py
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[5]
DB_FILE = REPO_ROOT / "_ops_smoke.db"
DATA_DIR = REPO_ROOT / "_ops_smoke_data"


def _env() -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO_ROOT / "src")
    env["DATABASE_URL"] = "sqlite:///./_ops_smoke.db"
    env["DATA_DIR"] = "./_ops_smoke_data"
    env["TAKUMI_MASTER_KEY"] = env.get("TAKUMI_MASTER_KEY") or "smoke-test-master-key-0123456789abcdef"
    return env


def _run(*args: str) -> str:
    result = subprocess.run(
        [sys.executable, "-m", "takumi_research_agent.cli", *args],
        cwd=REPO_ROOT,
        env=_env(),
        text=True,
        capture_output=True,
    )
    output = result.stdout.strip()
    try:
        payload = json.loads(output)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"CLI output is not JSON for {' '.join(args)}: {output!r}\n{result.stderr}") from exc
    if result.returncode != 0 and payload.get("blocked") is None:
        raise SystemExit(f"CLI failed for {' '.join(args)}: {payload}\n{result.stderr}")
    return json.dumps(payload)


# Seeding runs in its own process: the legacy Store never closes its sqlite handles, and on
# Windows an open handle makes the cleanup of the smoke data directory fail silently.
_SEED_CODE = """
import sys
from pathlib import Path
sys.path.insert(0, "src")
from takumi_research_agent.persistence.database import create_database
from takumi_research_agent.persistence.models import BlogProfile
from takumi_research_agent.store import Store

engine, sessions = create_database("sqlite:///./_ops_smoke.db")
with sessions.begin() as session:
    blog = BlogProfile(name="Smoke blog", base_url="https://blog.example/api", active=False, schedule_enabled=False)
    session.add(blog)
    session.flush()
    print(blog.id)
engine.dispose()

store = Store(Path("./_ops_smoke_data"))
store.save_paper("10.1234/smoke", {"title": "Smoke paper"}, "Smoke full text.")
store.replace_blog_posts([{"id": "42", "language": "id", "title": "Artikel lama", "content": "Isi"}])
store.save_publication("pub-1", "2026-11-01", "published", {"language": "id"}, {"id": "remote-9"})
store.set_job("2026-11-01", "completed")
store.save_draft("2026-11-01", {"articles": {"id": "draft"}}, {"decision": "draft"})
store.record_event("2026-11-01", "research_finished", {"sources": 1})
"""


def _seed() -> str:
    result = subprocess.run([sys.executable, "-c", _SEED_CODE], cwd=REPO_ROOT, env=_env(), text=True, capture_output=True)
    if result.returncode != 0:
        raise SystemExit(f"seed failed: {result.stderr}")
    return result.stdout.strip().splitlines()[-1]


def _rows(*statements: str) -> list[tuple]:
    import sqlite3

    connection = sqlite3.connect(DB_FILE)
    try:
        return [connection.execute(statement).fetchall() for statement in statements]
    finally:
        connection.close()


def main() -> int:
    os.chdir(REPO_ROOT)
    for path in (DB_FILE, DATA_DIR):
        shutil.rmtree(path, ignore_errors=True) if path.is_dir() else path.unlink(missing_ok=True)
    try:
        subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], cwd=REPO_ROOT, env=_env(), check=True, capture_output=True)
        blog_id = _seed()

        manifest = json.loads(_run("backup"))
        backup_path = manifest["path"]
        assert manifest["key_reference"] == "TAKUMI_MASTER_KEY", manifest
        print(f"PASS backup: {manifest['backup_id']} rows={sum(manifest['rows'].values())}")

        verified = json.loads(_run("verify-backup", backup_path))
        assert verified["ok"] is True, verified
        print(f"PASS verify-backup: schema={verified['schema_revision']}")

        imported = json.loads(_run("import-sqlite", str(DATA_DIR / "takumi.sqlite3"), "--blog-id", blog_id))
        assert imported["counts"]["papers"]["imported"] == 1, imported["counts"]
        assert imported["counts"]["publications"]["imported"] == 1, imported["counts"]
        assert imported["backup_id"], "import must take a backup first"
        assert imported["verification"]["artifacts_readable"] == 2, imported["verification"]
        print(f"PASS import-sqlite: counts={ {k: v['imported'] for k, v in imported['counts'].items()} }")

        again = json.loads(_run("import-sqlite", str(DATA_DIR / "takumi.sqlite3"), "--blog-id", blog_id))
        assert all(bucket["imported"] == 0 for bucket in again["counts"].values()), again["counts"]
        assert again["counts"]["papers"]["duplicates"] == 1, again["counts"]
        print("PASS import-sqlite idempotent: re-run reported only duplicates")

        refused = json.loads(_run("restore", backup_path))
        assert refused["blocked"] == "restore_requires_acknowledgement", refused
        print("PASS restore refuses without --acknowledge")

        report = json.loads(_run("restore", backup_path, "--acknowledge"))
        assert report["recovery_mode"] is True, report
        sources, control = _rows("select count(*) from sources", "select recovery_mode, content_mutations_paused from global_settings")
        assert sources[0][0] == 0, "restore must roll back the imported rows"
        assert control[0] == (1, 1), f"recovery mode/pause not forced: {control}"
        print(f"PASS restore: recovery_mode on, sources back to {sources[0][0]}")
        print("OK ops smoke")
        return 0
    finally:
        DB_FILE.unlink(missing_ok=True)
        for suffix in ("-wal", "-shm"):
            Path(str(DB_FILE) + suffix).unlink(missing_ok=True)
        shutil.rmtree(DATA_DIR, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
