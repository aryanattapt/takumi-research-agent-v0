# Evaluation and qualification

The evaluation commands score versioned inputs; they do not create ground truth. Keep development data separate from holdout data, snapshot the source material, and have humans adjudicate the labels before using a dataset for a release decision.

## Research and evidence quality

Add at least 40 cases across the four fields, with at least 10 per field and both development and holdout splits. Each case needs one or more source snapshots (`url`, SHA-256), human reviewer IDs, an adjudicated decision (`pass`, `fail`, or `uncertain`), and gold metric counts. The holdout must have three independently executed trials per case by default. Each trial's metrics need independent human evaluation with reviewer IDs; the model under evaluation cannot grade itself. Trial metadata binds the report to provider, model, prompt, tool schema, policy, adapter, code revision, and dataset versions.

Run:

```sh
python -m takumi_research_agent.evaluation research \
  --dataset evals/research-dataset-v1.json \
  --trials evals/research-trials-v1.json
```

The evaluator reports `incomplete` when the labels, version manifest, or trials are missing; it reports `failed` when a complete evaluation misses a gate. It will not convert an empty or synthetic fixture into a pass. Initial numeric gates follow `docs/planning/plan.md` section 17.3: zero critical unsupported/contradicted claims, fabricated sources/quotes, or journal-policy bypasses; at least 95% citation support; 100% numeric/date/unit parity; and reader score 4+ on at least 80% of trial outputs.

## Retrieval, trend ranking, and deduplication

The versioned corpus has three case kinds:

- `retrieval`: a query, supported field, a nonempty set of relevant history IDs, holdout split, adjudicators, source snapshots, and at least 200 distractors (`corpus_size` >= 201 including a relevant result).
- `ranking`: a query and human relevance grades from 0 to 3 for candidate IDs.
- `dedup`: a pair, `gold_duplicate`, cross-language flag, adjudicators, and source snapshots.

The initial suite requires 40 queries per retrieval/ranking kind and 40 dedup pairs, at least 20 cross-language pairs. It reports recall@20, NDCG@20, and duplicate precision/recall. The release gates in section 17.3 are 95% recall@20 and 95% duplicate precision and recall.

```sh
python -m takumi_research_agent.evaluation retrieval \
  --dataset evals/retrieval-dataset-v1.json \
  --results evals/retrieval-results-v1.json
```

## Reviewer calibration

Provide reviewer decisions against the dataset's adjudicated decisions. The tool reports accuracy and Cohen's kappa per reviewer and requires 20 overlapping labels by default. It does not invent a pass threshold; the project owner must choose one before using the result to grant reviewer authority.

```sh
python -m takumi_research_agent.evaluation reviewer \
  --dataset evals/research-dataset-v1.json \
  --labels evals/reviewer-labels-v1.json
```

## Data formats

See `evals/*-template-v1.json` for empty versioned shells. A research case has this shape:

```json
{
  "case_id": "bio-001",
  "split": "holdout",
  "field": "biology",
  "source_snapshots": [{"url": "https://source.example/paper", "sha256": "<64 hex characters>"}],
  "adjudication": {"adjudicated": true, "reviewers": ["reviewer-a", "reviewer-b"], "decision": "pass"},
  "gold": {
    "critical_unsupported_claims": 0,
    "contradicted_critical_claims": 0,
    "fabricated_sources": 0,
    "fabricated_quotes": 0,
    "journal_policy_bypasses": 0,
    "supported_citations": 19,
    "citation_total": 20,
    "numeric_date_unit_parity_failures": 0,
    "numeric_date_unit_parity_checks": 4,
    "reader_score": 4
  }
}
```

Trial result rows use the same metric keys and add `case_id`, `trial_index` (starting at 1), a `metadata` object containing all eight version fields, and `evaluation: {"adjudicated": true, "reviewers": ["reviewer-id"]}`. Keep trial summaries linked to detailed traces/artifacts outside the JSON file, with access controls appropriate for API and model output data.

## Current qualification state

The checked-in files are templates, not labeled benchmark data. No real provider/model multi-trial run or independent reviewer calibration is included. Until those inputs are populated and evaluated, M4 retrieval quality and M5 research/editorial quality remain unqualified.
