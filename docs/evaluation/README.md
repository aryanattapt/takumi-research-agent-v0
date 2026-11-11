# Evaluation and qualification

The evaluation commands score versioned inputs; they do not create ground truth. Keep development data separate from holdout data, snapshot the source material, and have humans adjudicate the labels before using a dataset for a release decision.

## Run the research agent

The run-research command executes the existing read-only research agent against cases in a dataset. It makes live model and public-source requests and consumes provider tokens. It does not call a blog API. Use `--concurrency 1` by default; values up to 10 let an operator measure a controlled concurrent run after checking the selected provider quota. This measures one evaluation process, not the complete multi-worker deployment.

Each runnable case needs topic, field, history_complete, blog_history, and optional trend_metrics. Set history_complete explicitly: use false when the blog snapshot is incomplete and true only when an empty or populated snapshot is known to be complete. Keep benchmark corpora, source snapshots, and gold labels out of the run output directory.

    export OPENAI_API_KEY="..."
    export TAKUMI_EVAL_MODEL="provider-model-name"
    python -m takumi_research_agent.evaluation run-research \
      --dataset evals/research-dataset-v1.json \
      --output data/evaluations/research-runs.json \
      --provider openai-compatible \
      --model "$TAKUMI_EVAL_MODEL" \
      --prompt-version research-prompt-v1 \
      --split holdout \
      --trials-per-case 3

For Anthropic, use --provider anthropic --model MODEL_NAME and set ANTHROPIC_API_KEY. The --api-key-env option selects a different environment variable. The --base-url option supports a compatible endpoint. The --browser option enables the local Playwright fallback and requires the browser extra. Runs are sequential to avoid uncontrolled parallel load. The output records duration, physical request/tool counts, token usage, version metadata, trace, research bundle, sources, and evidence. Use --resume to continue a compatible run after interruption; provider/model/prompt/tool/policy/adapter/code/dataset metadata must match. The code revision is read from git; in a deployment without git metadata, set TAKUMI_CODE_REVISION or pass --code-revision explicitly.

Run output and generated artifacts can contain model responses, source excerpts, and evidence. Keep them out of public commits and follow the access controls used for other research data.

## Human review workflow

Create a review packet from the raw run file:

    python -m takumi_research_agent.evaluation review-packet \
      --dataset evals/research-dataset-v1.json \
      --runs data/evaluations/research-runs.json \
      --output data/evaluations/research-review.json

The packet omits gold labels and provider/model metadata from the reviewer view. A reviewer fills review.adjudicated, review.reviewers, and each metric in review.metrics. The review must be independent of the model being measured. Then merge those annotations with the immutable run manifest:

    python -m takumi_research_agent.evaluation build-trials \
      --dataset evals/research-dataset-v1.json \
      --runs data/evaluations/research-runs.json \
      --reviews data/evaluations/research-review.json \
      --output data/evaluations/research-trials-v1.json

    python -m takumi_research_agent.evaluation research \
      --dataset evals/research-dataset-v1.json \
      --trials data/evaluations/research-trials-v1.json

Incomplete annotations stay incomplete; the merger does not fill metrics or create adjudications. Runs that did not produce a completed research bundle are blocked from qualification. The qualification evaluator still requires the independently sourced and human-labeled dataset described below.

## Research and evidence quality

Use separate development cases and at least 40 human-labeled holdout cases, with at least 10 holdout cases per field. Each case needs one or more source snapshots (`url`, SHA-256), human reviewer IDs, an adjudicated decision (`pass`, `fail`, or `uncertain`), and gold metric counts. The holdout must have three independently executed trials per case by default. Each trial's metrics need independent human evaluation with reviewer IDs; the model under evaluation cannot grade itself. Trial metadata binds the report to provider, model, prompt, tool schema, policy, adapter, code revision, and dataset versions. Count metrics and reader scores are whole-number labels; fractional counts are rejected.

Run:

```sh
python -m takumi_research_agent.evaluation research \
  --dataset evals/research-dataset-v1.json \
  --trials evals/research-trials-v1.json
```

The evaluator reports `incomplete` when the labels, version manifest, or trials are missing; it reports `failed` when a complete evaluation misses a gate. It will not convert an empty or synthetic fixture into a pass. Initial numeric gates follow `docs/planning/plan.md` section 17.3: zero critical unsupported/contradicted claims, fabricated sources/quotes, or journal-policy bypasses; at least 95% citation support; 100% numeric/date/unit parity; and reader score 4+ on at least 80% of trial outputs.

## Retrieval, trend ranking, and deduplication

The versioned corpus has three case kinds:

- `retrieval`: a query, supported field, a nonempty set of relevant history IDs, development or holdout split, adjudicators, source snapshots, and at least 200 distractors (`corpus_size` >= 201 including a relevant result).
- `ranking`: a query and human relevance grades from 0 to 3 for candidate IDs.
- `dedup`: a pair, `gold_duplicate`, cross-language flag, adjudicators, and source snapshots.

The release holdout requires 40 queries per retrieval/ranking kind and 40 dedup pairs, including at least 20 cross-language pairs. It reports recall@20, NDCG@20, and duplicate precision/recall. The release gates in section 17.3 are 95% recall@20 and 95% duplicate precision and recall. Development datasets may contain only the case kinds they measure; they produce a `development_scored` report and can never qualify a release.

The benchmark corpus is stored once at the top level of the dataset as corpus, with records containing id, title, and optional summary, content, and language. Retrieval/ranking cases need query; dedup cases need left_id and right_id references into that corpus. Generate predictions with:

    python -m takumi_research_agent.evaluation run-retrieval \
      --dataset evals/retrieval-dataset-v1.json \
      --output data/evaluations/retrieval-results-v1.json \
      --duplicate-threshold 0.10

    python -m takumi_research_agent.evaluation retrieval \
      --dataset evals/retrieval-dataset-v1.json \
      --results data/evaluations/retrieval-results-v1.json

This runner measures the current lexical Jaccard retriever and records its version. It is a baseline, not the planned multilingual semantic index. Run the evaluator on development-only cases while tuning the duplicate threshold, then freeze it before scoring a separate holdout dataset. A complete development evaluation reports `development_scored`; it is deliberately never a release qualification. A complete holdout evaluation reports `qualified` only when every gate passes. Results are bound to the canonical dataset fingerprint and corpus IDs; regenerate results after any dataset change.

### Optional public Indonesian retrieval development set

MIRACL publishes human-annotated Indonesian retrieval queries and relevance judgments. Use the public `dev` split as an external development baseline only; it is monolingual Wikipedia retrieval, not a Takumi science-history holdout, and contains no deduplication labels. The importer requires an exported JSONL file with the MIRACL dataset-card fields `query_id`, `query`, `positive_passages`, and `negative_passages`; each passage contains `docid`, `title`, and `text`. It keeps the first 40 usable queries, fingerprints the export bytes, and rejects an undersized corpus.

Export rows locally from the `miracl/miracl`, Indonesian (`id`), `dev` split, saving one JSON object per line. Keep any Hugging Face access token in the local environment; do not commit the exported dataset or token. Then run:

```sh
python -m takumi_research_agent.evaluation import-miracl-dev \
  --input-jsonl data/evaluations/miracl-id-dev.jsonl \
  --output data/evaluations/miracl-id-dev-v1.json

python -m takumi_research_agent.evaluation run-retrieval \
  --dataset data/evaluations/miracl-id-dev-v1.json \
  --output data/evaluations/miracl-id-dev-results.json

python -m takumi_research_agent.evaluation retrieval \
  --dataset data/evaluations/miracl-id-dev-v1.json \
  --results data/evaluations/miracl-id-dev-results.json
```

The import records MIRACL's annotation panel as the public label source and the input file SHA-256. The dataset card identifies the dataset as Apache-2.0; check the underlying corpus terms before redistributing corpus text. Public development scores are useful for comparing retrievers, but do not replace internally adjudicated holdout labels or human review for research quality.

### Embedding-backed retrieval benchmark

To compare a semantic retrieval model on the same versioned corpus, run the optional OpenAI-compatible embeddings path:

```sh
python -m takumi_research_agent.evaluation run-semantic-retrieval \
  --dataset evals/retrieval-dataset-v1.json \
  --output data/evaluations/retrieval-semantic-v1.json \
  --embedding-model YOUR_MULTILINGUAL_EMBEDDING_MODEL \
  --api-key-env OPENAI_API_KEY \
  --batch-size 64 \
  --duplicate-threshold 0.80

python -m takumi_research_agent.evaluation retrieval \
  --dataset evals/retrieval-dataset-v1.json \
  --results data/evaluations/retrieval-semantic-v1.json
```

Choose an embeddings endpoint/model that supports Indonesian-English cross-language matching. The runner embeds the dataset corpus and queries in batches, ranks by cosine similarity, and evaluates dedup pairs with the same scorer as the lexical baseline. Its in-memory index is rebuilt for every invocation. The production pipeline has a separate opt-in cache keyed by blog, endpoint/model, and content hash. Tune the threshold on development labels, then freeze it for holdout. A complete development run is scored for comparison only and cannot qualify a release. Corpus text, queries, and enabled blog history are sent to the configured endpoint; do not send private content without approval.

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
