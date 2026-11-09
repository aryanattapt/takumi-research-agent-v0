from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import socket
from pathlib import Path
from uuid import uuid4

import uvicorn
from alembic import command
from alembic.config import Config

from takumi_research_agent.config import Settings
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


def main() -> None:
    parser = argparse.ArgumentParser(prog="takumi", description="Takumi bilingual research agent")
    parser.add_argument("command", choices=("api", "worker", "migrate"), help="service to start or migration action")
    args = parser.parse_args()
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper(), format="%(asctime)s %(levelname)s %(message)s")
    settings = Settings.from_env()

    if args.command == "migrate":
        command.upgrade(_alembic_config(), "head")
    elif args.command == "api":
        from takumi_research_agent.api.app import create_app

        app = create_app(settings)
        uvicorn.run(app, host=os.getenv("API_HOST", "0.0.0.0"), port=int(os.getenv("API_PORT", "8000")), proxy_headers=True)
    else:
        asyncio.run(_worker_loop(settings))


if __name__ == "__main__":
    main()
