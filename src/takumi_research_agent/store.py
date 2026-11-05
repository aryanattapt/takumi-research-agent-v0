from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.data_dir / "takumi.sqlite3"
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.db_path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON")
        return db

    def _initialize(self) -> None:
        with self._connect() as db:
            db.execute("PRAGMA journal_mode = WAL")
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    slot TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    memory TEXT NOT NULL DEFAULT '{}',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    retry_at TEXT,
                    error TEXT,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS papers (
                    doi TEXT PRIMARY KEY,
                    record TEXT NOT NULL,
                    content TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS blog_posts (
                    post_id TEXT NOT NULL,
                    language TEXT NOT NULL DEFAULT '',
                    topic_id TEXT NOT NULL DEFAULT '',
                    title TEXT NOT NULL,
                    content TEXT NOT NULL DEFAULT '',
                    doi TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL DEFAULT '',
                    record TEXT NOT NULL,
                    PRIMARY KEY (post_id, language)
                );
                CREATE TABLE IF NOT EXISTS drafts (
                    slot TEXT PRIMARY KEY,
                    bundle TEXT NOT NULL,
                    review TEXT NOT NULL DEFAULT '{}',
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS publications (
                    publication_key TEXT PRIMARY KEY,
                    slot TEXT NOT NULL,
                    status TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    response TEXT NOT NULL DEFAULT '{}',
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS agent_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    slot TEXT NOT NULL,
                    event TEXT NOT NULL,
                    details TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )

    def get_job(self, slot: str) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM jobs WHERE slot = ?", (slot,)).fetchone()
        if row is None:
            return None
        item = dict(row)
        item["memory"] = json.loads(item["memory"])
        return item

    def set_job(self, slot: str, status: str, *, memory: dict | None = None, error: str | None = None) -> None:
        with self._connect() as db:
            db.execute(
                """
                INSERT INTO jobs(slot, status, memory, attempts, error, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(slot) DO UPDATE SET
                    status=excluded.status,
                    memory=CASE WHEN ? THEN excluded.memory ELSE jobs.memory END,
                    attempts=jobs.attempts + CASE
                        WHEN excluded.status = 'researching' AND jobs.status != 'researching' THEN 1
                        ELSE 0
                    END,
                    error=excluded.error,
                    updated_at=excluded.updated_at
                """,
                (
                    slot,
                    status,
                    json.dumps(memory or {}, ensure_ascii=False),
                    1 if status == "researching" else 0,
                    error,
                    _now(),
                    1 if memory is not None else 0,
                ),
            )

    def save_paper(self, doi: str, record: dict, content: str = "") -> None:
        with self._connect() as db:
            db.execute(
                """
                INSERT INTO papers(doi, record, content, updated_at) VALUES (?, ?, ?, ?)
                ON CONFLICT(doi) DO UPDATE SET
                    record=excluded.record,
                    content=CASE WHEN excluded.content = '' THEN papers.content ELSE excluded.content END,
                    updated_at=excluded.updated_at
                """,
                (doi, json.dumps(record, ensure_ascii=False), content, _now()),
            )

    def get_paper(self, doi: str) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute("SELECT record, content FROM papers WHERE doi = ?", (doi,)).fetchone()
        if row is None:
            return None
        result = json.loads(row["record"])
        result["content"] = row["content"]
        return result

    def replace_blog_posts(self, posts: list[dict]) -> None:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM blog_posts")
            db.executemany(
                """
                INSERT OR REPLACE INTO blog_posts(
                    post_id, language, topic_id, title, content, doi, updated_at, record
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        str(post.get("id", "")),
                        str(post.get("language", "")),
                        str(post.get("topic_id", "")),
                        str(post.get("title", "")),
                        str(post.get("content", "")),
                        str(post.get("doi", "")),
                        str(post.get("updated_at", "")),
                        json.dumps(post, ensure_ascii=False),
                    )
                    for post in posts
                ],
            )

    def get_blog_posts(self) -> list[dict]:
        with self._connect() as db:
            rows = db.execute("SELECT record FROM blog_posts ORDER BY updated_at DESC").fetchall()
        return [json.loads(row["record"]) for row in rows]

    def save_draft(self, slot: str, bundle: dict, review: dict | None = None) -> None:
        with self._connect() as db:
            db.execute(
                """
                INSERT INTO drafts(slot, bundle, review, updated_at) VALUES (?, ?, ?, ?)
                ON CONFLICT(slot) DO UPDATE SET
                    bundle=excluded.bundle, review=excluded.review, updated_at=excluded.updated_at
                """,
                (
                    slot,
                    json.dumps(bundle, ensure_ascii=False),
                    json.dumps(review or {}, ensure_ascii=False),
                    _now(),
                ),
            )

    def save_publication(self, publication_key: str, slot: str, status: str, payload: dict, response: dict | None = None) -> None:
        with self._connect() as db:
            db.execute(
                """
                INSERT INTO publications(publication_key, slot, status, payload, response, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(publication_key) DO UPDATE SET
                    status=excluded.status, response=excluded.response, updated_at=excluded.updated_at
                """,
                (
                    publication_key,
                    slot,
                    status,
                    json.dumps(payload, ensure_ascii=False),
                    json.dumps(response or {}, ensure_ascii=False),
                    _now(),
                ),
            )

    def record_event(self, slot: str, event: str, details: dict) -> None:
        with self._connect() as db:
            db.execute(
                "INSERT INTO agent_events(slot, event, details, created_at) VALUES (?, ?, ?, ?)",
                (slot, event, json.dumps(details, ensure_ascii=False), _now()),
            )

    def events(self, slot: str) -> list[dict]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT event, details, created_at FROM agent_events WHERE slot = ? ORDER BY id",
                (slot,),
            ).fetchall()
        return [{**dict(row), "details": json.loads(row["details"])} for row in rows]


class RunLock:
    def __init__(self, data_dir: Path):
        self.path = data_dir / "run.lock"
        self.owned = False

    def __enter__(self) -> "RunLock":
        import os
        import time

        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                age = time.time() - self.path.stat().st_mtime
                if age > 6 * 60 * 60:
                    self.path.unlink()
                    fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                else:
                    raise RuntimeError(f"Another run holds {self.path}")
            except FileNotFoundError:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(str(os.getpid()))
        self.owned = True
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        if self.owned:
            self.path.unlink(missing_ok=True)
