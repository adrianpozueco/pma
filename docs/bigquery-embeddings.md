# Retrieval documents and embeddings

The F2 retrieval corpus is a versioned local artifact. It keeps AMOS and FAA
text in one pinned embedding space while retaining their source namespaces and
availability rules. It does not change the raw snapshot, train a model, create
a Vector Search endpoint, or change the application's chat Gemini model.

`gemini-embedding-001`, version `001`, and 3072 dimensions are the default
contract. Documents use `RETRIEVAL_DOCUMENT`; runtime queries use the same
configuration with `RETRIEVAL_QUERY`. The preparation version and reference
corpus version are part of the cache key, so output from another model,
dimension, task, preparation, or corpus cannot be reused.

Prepare a small local artifact first:

```sh
uv run python scripts/prepare_retrieval_documents.py \
  --output /tmp/retrieval_documents.jsonl \
  --report /tmp/retrieval_documents_coverage.json
```

The command defaults to ten eligible reference rows. Add `--all` only after reviewing the coverage
report. The AMOS source hash must match the immutable split manifest. It emits
one work-step description (or a headline only when the description is absent),
records chunk source offsets, and never appends action, diagnosis, or repair
text to a symptom document. Final-test, purged, duplicate, and index-ineligible
AMOS workorders are omitted.

Closed AMOS text is marked `snapshot_only` and becomes historical evidence only
after its envelope-export timestamp. FAA `SubmissionDate` and difficulty date
are not publication proof. Therefore raw FAA discrepancies are retained as
`context_only` by default and are not embedded. To make a separately supplied
FAA snapshot usable as historical context, record its actual, independently
verified export time:

```sh
uv run python scripts/prepare_retrieval_documents.py \
  --faa-snapshot-as-of ACTUAL_VERIFIED_EXPORT_TIME --all \
  --output /tmp/retrieval_documents.jsonl \
  --report /tmp/retrieval_documents_coverage.json
```

FAA aircraft model values remain unmapped (`aircraft_family = NULL`) until a
reviewed FAA-to-AMOS mapping exists. The full narrative is a historical case;
it is not presented as a verified early symptom.

Embedding is dry-run by default and limited to ten documents:

```sh
uv run python scripts/embed_retrieval_documents.py \
  --documents /tmp/retrieval_documents.jsonl
```

Use a bounded live batch only with explicit credentials and project:

```sh
uv run python scripts/embed_retrieval_documents.py \
  --documents /tmp/retrieval_documents.jsonl --limit 10 --run \
  --project YOUR_PROJECT --cache /tmp/retrieval_embedding_cache.jsonl \
  --output /tmp/retrieval_embeddings.jsonl
```

The Vertex call uses `EmbedContentConfig(auto_truncate=False)`. Gemini's
individual-input limit is 2048 tokens; a long text is split at source
boundaries first and any remaining over-limit input fails visibly instead of
being silently truncated. Successful vectors are appended to the JSONL cache.
Malformed, non-finite, empty, or wrong-dimension responses become failed rows
with an error code and cause a nonzero command result. Failed rows are never
put in the success cache.

Writing BigQuery rows is separate from generating embeddings:

```sh
uv run python scripts/embed_retrieval_documents.py \
  --documents /tmp/retrieval_documents.jsonl --limit 10 --run \
  --project YOUR_PROJECT --dataset pma_agent_analytics --table retrieval_embeddings \
  --write-bigquery
```

The document writer is separately explicit and uses the matching table:

```sh
uv run python scripts/prepare_retrieval_documents.py --all \
  --project YOUR_PROJECT --dataset pma_agent_analytics --table retrieval_documents \
  --write-bigquery --max-write-rows 200000
```

Both writers accept only their configured F2 table and use bounded staging plus
`MERGE` on the corpus/config identity, so exact reruns do not create duplicates.
Writing documents requires `--all` (or `--limit 0`) so a partial local sample
cannot become a serving corpus.
Both writers default to a 10,000-row write budget and 500-row staging loads.
Increase `--max-write-rows` only after reviewing the generated coverage report;
the argument is an explicit cap, not a request to fetch or embed more rows.
BigQuery load and merge jobs run in `us-central1`, use a 20-second request
deadline, a 60-second job deadline, no client retries, and a 1 GB merge
byte-billing cap. Each invocation uses a unique staging table and removes it
after the merge.
They write only dimension-validated successful embeddings. Cached vectors are
persisted as `success`; cache counts remain in the run report. Neither writer
builds an index; serving uses materialized BigQuery rows through the runtime
retrieval implementation.
