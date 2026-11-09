from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
import socket
from pathlib import Path
from uuid import uuid4

from alembic import command
from alembic.config import Config

from takumi_research_agent.config import Settings
from takumi_research_agent.operations.backup import BackupBlocked, create_backup, restore_backup, verify_backup
from takumi_research_agent.operations.legacy_import import LegacyImportBlocked, import_legacy_sqlite
from takumi_research_agent.research.artifacts import ArtifactStore
from takumi_research_agent.orchestration.capacity import DEFAULT_RUN_MINUTES, MAX_CONCURRENCY, MAX_RUN_MINUTES, MIN_RUN_MINUTES, apply_admission_control
from takumi_research_agent.orchestration.pipeline import DailyResearchProcessor
from takumi_research_agent.orchestration.scheduler import RunWorker, enqueue_due_slots
from takumi_research_agent.persistence.database import create_database


def _alembic_config() -> Config:
    project_root = Path(__file__).resolve().parents[2]
    config = Config(str(project_root / "alembic.ini"))
    config.set_main_option("script_location", str(project_root / "migrations"))
    return config


async def _worker_loop(settings: Settings) -> None:
    engine, sessions = create_database(settings.database_url)
    worker_id = f"{socket.gethostname()}-{os.getpid()}-{uuid4().hex[:8]}"
    processor = DailyResearchProcessor(sessions, settings)
    concurrency = min(max(int(os.getenv("WORKER_CONCURRENCY", "2")), 1), MAX_CONCURRENCY)
    run_minutes = min(max(int(os.getenv("CAPACITY_RUN_MINUTES", str(DEFAULT_RUN_MINUTES))), MIN_RUN_MINUTES), MAX_RUN_MINUTES)
    worker = RunWorker(sessions, worker_id=worker_id, processor=processor, concurrency=concurrency)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signum, stop.set)
        except NotImplementedError:
            pass
    logging.info("Takumi worker started (%s, concurrency=%s)", worker_id, concurrency)
    try:
        while not stop.is_set():
            with sessions.begin() as session:
                created = enqueue_due_slots(session, concurrency=concurrency, run_minutes=run_minutes)
            if created:
                logging.info("Enqueued %s scheduled run(s)", len(created))
            with sessions.begin() as session:
                plan = apply_admission_control(session, concurrency=concurrency, run_minutes=run_minutes)
            if plan["counts"]["at_risk"] or plan["counts"]["infeasible"]:
                logging.warning(
                    "Admission control: %s at_risk, %s infeasible of %s pending (concurrency=%s, %s min/run)",
                    plan["counts"]["at_risk"],
                    plan["counts"]["infeasible"],
                    plan["pending"],
                    concurrency,
                    run_minutes,
                )
            results = await asyncio.gather(*(worker.run_once() for _ in range(concurrency)))
            if not any(results):
                try:
                    await asyncio.wait_for(stop.wait(), timeout=5)
                except TimeoutError:
                    pass
    finally:
        await processor.close()
        engine.dispose()
        logging.info("Takumi worker stopped")


def _backup_command(settings: Settings) -> int:
    engine, sessions = create_database(settings.database_url)
    try:
        manifest = create_backup(
            sessions,
            artifacts_root=settings.data_dir / "artifacts",
            output_dir=settings.data_dir / "backups",
        )
        print(json.dumps({
            "backup_id": manifest["backup_id"],
            "path": str((settings.data_dir / "backups" / manifest["backup_id"]).resolve()),
            "schema_revision": manifest["schema_revision"],
            "rows": manifest["database"]["rows"],
            "artifacts": len(manifest["artifacts"]),
            "key_reference": manifest["key_reference"],
        }, indent=2))
    except BackupBlocked as exc:
        print(json.dumps({"blocked": exc.code, "detail": exc.detail}, indent=2))
        return 1
    finally:
        engine.dispose()
    return 0


def _verify_command(target: str) -> int:
    result = verify_backup(Path(target))
    print(json.dumps(result, indent=2))
    return 0 if result["ok"] else 1


def _restore_command(settings: Settings, target: str, acknowledge: bool) -> int:
    engine, sessions = create_database(settings.database_url)
    try:
        with sessions.begin() as session:
            report = restore_backup(
                session,
                backup_path=Path(target),
                artifacts_root=settings.data_dir / "artifacts",
                master_key=settings.master_key,
                acknowledge=acknowledge,
            )
        print(json.dumps(report, indent=2))
    except BackupBlocked as exc:
        print(json.dumps({"blocked": exc.code, "detail": exc.detail}, indent=2))
        return 1
    finally:
        engine.dispose()
    return 0


def _import_sqlite_command(settings: Settings, target: str, blog_id: str | None, dry_run: bool) -> int:
    if not blog_id:
        print(json.dumps({"blocked": "blog_id_required", "detail": "legacy rows must be mapped to one blog profile"}, indent=2))
        return 1
    engine, sessions = create_database(settings.database_url)
    artifacts_root = settings.data_dir / "artifacts"
    try:
        backup_id = None
        if not dry_run:
            # Plan §12.6: back up the current database and keep the legacy file untouched.
            backup_id = create_backup(sessions, artifacts_root=artifacts_root, output_dir=settings.data_dir / "backups")["backup_id"]
        with sessions.begin() as session:
            report = import_legacy_sqlite(
                session,
                database_path=Path(target),
                blog_id=blog_id,
                artifacts=ArtifactStore(artifacts_root),
                dry_run=dry_run,
            )
        print(json.dumps({**report, "backup_id": backup_id}, indent=2))
    except (BackupBlocked, LegacyImportBlocked) as exc:
        print(json.dumps({"blocked": exc.code, "detail": exc.detail}, indent=2))
        return 1
    finally:
        engine.dispose()
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(prog="takumi", description="Takumi bilingual research agent")
    parser.add_argument(
        "command",
        choices=("api", "worker", "migrate", "backup", "verify-backup", "restore", "import-sqlite"),
        help="service to start or operational action",
    )
    parser.add_argument("target", nargs="?", help="backup directory, manifest.json, or legacy takumi.sqlite3 path")
    parser.add_argument("--acknowledge", action="store_true", help="required by restore: replace local data with the backup")
    parser.add_argument("--blog-id", dest="blog_id", help="blog profile that owns every imported legacy record")
    parser.add_argument("--dry-run", dest="dry_run", action="store_true", help="import-sqlite: report what would be imported and change nothing")
    args = parser.parse_args()
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper(), format="%(asctime)s %(levelname)s %(message)s")
    settings = Settings.from_env()

    if args.command in {"verify-backup", "restore", "import-sqlite"} and not args.target:
        parser.error(f"{args.command} requires a path argument")

    if args.command == "migrate":
        command.upgrade(_alembic_config(), "head")
    elif args.command == "backup":
        raise SystemExit(_backup_command(settings))
    elif args.command == "verify-backup":
        raise SystemExit(_verify_command(args.target))
    elif args.command == "restore":
        raise SystemExit(_restore_command(settings, args.target, args.acknowledge))
    elif args.command == "import-sqlite":
        raise SystemExit(_import_sqlite_command(settings, args.target, args.blog_id, args.dry_run))
    elif args.command == "api":
        import uvicorn

        from takumi_research_agent.api.app import create_app

        app = create_app(settings)
        uvicorn.run(app, host=os.getenv("API_HOST", "0.0.0.0"), port=int(os.getenv("API_PORT", "8000")), proxy_headers=True)
    else:
        asyncio.run(_worker_loop(settings))


if __name__ == "__main__":
    main()
