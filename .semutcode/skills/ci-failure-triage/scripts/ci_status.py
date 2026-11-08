#!/usr/bin/env python3
"""Inspect GitHub Actions runs without gh. Read-only; job logs require admin."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request


def repo_from_git() -> str:
    root = os.getcwd()
    url = subprocess.check_output(
        ["git", "-c", f"safe.directory={root}", "remote", "get-url", "origin"],
        text=True,
    ).strip()
    if url.startswith("git@github.com:"):
        path = url.split(":", 1)[1]
    elif "github.com/" in url:
        path = url.split("github.com/", 1)[1]
    else:
        raise SystemExit(f"Cannot derive GitHub repo from origin: {url}")
    return path.removesuffix(".git").strip("/")


def get_json(url: str):
    request = urllib.request.Request(
        url,
        headers={"Accept": "application/vnd.github+json", "User-Agent": "ci-status-skill"},
    )
    last_error: object = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                return json.load(response)
        except OSError as exc:
            last_error = exc
            if attempt < 2:
                time.sleep(2 * (attempt + 1))
    raise SystemExit(f"GitHub API request failed after retries: {last_error}")


def find_run(repo: str, sha: str | None):
    runs = get_json(f"https://api.github.com/repos/{repo}/actions/runs?per_page=20").get("workflow_runs", [])
    if sha:
        runs = [run for run in runs if run["head_sha"].startswith(sha)]
    return runs[0] if runs else None


def print_run(repo: str, run: dict) -> None:
    print(f"RUN {run['id']} {run['name']} {run['head_sha'][:7]} {run['status']} {run['conclusion']}")
    jobs = get_json(f"https://api.github.com/repos/{repo}/actions/runs/{run['id']}/jobs").get("jobs", [])
    for job in jobs:
        print(f"  JOB {job['name']}: {job['conclusion']}")
        for step in job.get("steps", []):
            if step.get("conclusion") not in ("success", "skipped", None):
                print(f"    FAILED STEP {step['number']}: {step['name']} ({step['conclusion']})")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", help="owner/name; default comes from git origin")
    parser.add_argument("--sha", help="filter by head SHA prefix")
    parser.add_argument("--wait", action="store_true", help="poll until the run completes")
    parser.add_argument("--timeout", type=int, default=600, help="wait timeout in seconds")
    args = parser.parse_args()

    repo = args.repo or repo_from_git()
    deadline = time.time() + args.timeout
    while True:
        run = find_run(repo, args.sha)
        if run is None:
            print(f"No workflow run found for {repo} sha={args.sha or '*'}")
            sys.exit(1)
        if run["status"] == "completed" or not args.wait:
            print_run(repo, run)
            sys.exit(0 if run["conclusion"] == "success" else 2)
        if time.time() >= deadline:
            print(f"Timed out waiting for run {run['id']} ({run['status']})")
            sys.exit(3)
        time.sleep(15)


if __name__ == "__main__":
    main()
