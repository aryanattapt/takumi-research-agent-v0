from __future__ import annotations

import argparse
import json
import sys

from .qualification import calibrate_reviewers, qualify_research_dataset, read_json
from .retrieval import evaluate_retrieval_benchmark


def main() -> int:
    parser = argparse.ArgumentParser(description="Run versioned offline Takumi evaluation suites")
    subparsers = parser.add_subparsers(dest="suite", required=True)
    research = subparsers.add_parser("research", help="qualify labeled research/evidence holdout trials")
    research.add_argument("--dataset", required=True)
    research.add_argument("--trials", required=True)
    research.add_argument("--minimum-cases", type=int, default=40)
    research.add_argument("--trials-per-case", type=int, default=3)
    retrieval = subparsers.add_parser("retrieval", help="score labeled history retrieval, ranking, and dedup")
    retrieval.add_argument("--dataset", required=True)
    retrieval.add_argument("--results", required=True)
    reviewer = subparsers.add_parser("reviewer", help="measure reviewer agreement against adjudication")
    reviewer.add_argument("--dataset", required=True)
    reviewer.add_argument("--labels", required=True)
    reviewer.add_argument("--minimum-labels", type=int, default=20)
    args = parser.parse_args()

    dataset = read_json(args.dataset)
    if args.suite == "research":
        report = qualify_research_dataset(
            dataset,
            read_json(args.trials),
            minimum_cases=args.minimum_cases,
            trials_per_case=args.trials_per_case,
        )
        exit_code = 0 if report["qualified"] else 2
    elif args.suite == "retrieval":
        report = evaluate_retrieval_benchmark(dataset, read_json(args.results))
        exit_code = 0 if report["qualified"] else 2
    else:
        labels_document = read_json(args.labels)
        adjudicated = {
            case["case_id"]: case["adjudication"]["decision"]
            for case in dataset.get("cases", [])
            if isinstance(case, dict)
            and isinstance(case.get("case_id"), str)
            and isinstance(case.get("adjudication"), dict)
            and isinstance(case["adjudication"].get("decision"), str)
        }
        report = calibrate_reviewers(adjudicated, labels_document.get("labels", []), minimum_labels_per_reviewer=args.minimum_labels)
        exit_code = 0 if report["status"] == "measured" else 2
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
