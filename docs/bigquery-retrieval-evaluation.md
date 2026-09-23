# Historical retrieval evaluation

Run the local evaluator only with an independently reviewed JSONL relevance set.
Each row needs `query_id`, `target_part_number`, `symptoms`, timezone-aware
`analysis_as_of`, `corpus_version`, `expected_relevant_case_ids`, and
`review_status: "reviewed"`. Case IDs are source-aware, for example
`amos:<record_id>` or `faa_sdr:<record_id>`.

```sh
uv run --offline --no-sync python scripts/evaluate_history_retrieval.py \
  --documents /path/retrieval_documents.jsonl --labels /path/reviewed.jsonl --k 5
```

Use `--amos-only` for the AMOS-only comparison; the default includes AMOS and FAA.
The report gives per-PN and overall precision@k, recall@k, MRR, reviewed no-match
accuracy and missing target support. It refuses missing corpora, unreviewed labels,
invalid source IDs and retrieval errors. These are retrieval
relevance metrics only, never predictive-model, failure-rate, timing, or policy metrics.

The default is credential-free keyword ranking. For `--method vector` or `hybrid`,
pass `--embedding-config` JSON for the pinned `EmbeddingConfig`; every reviewed
label must include `query_embedding` and matching `query_embedding_metadata`, and
documents must carry compatible precomputed vectors. The harness never calls Vertex,
BigQuery, or a chat model.

Rows with `expected_relevant_case_ids: []` are reviewed no-match judgments. They
contribute to no-match accuracy; recall and MRR are `null` when a
group has no relevant judgments. Final evaluation accepts only the three fixed target
PNs and preserves the same judged denominator for the AMOS-only ablation, reporting
FAA support excluded by that scope.
