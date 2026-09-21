# PMA-POC — Offline Preparation Implementation Plan

> **Jira ID:** `PMA-POC` (placeholder — replace with real ticket key)
> **Scope:** Offline preparation only (requirements §6 step 2) — the batch pipeline that runs
> **before** any online prediction. It turns raw AMOS work-order XML into the durable artifacts
> the online agent (requirements §7 Steps 1–7) consumes.
> **Platform:** Google Cloud — GCS + BigQuery + Vertex AI. Provisioned via `infra/`.
> **Deliverable type:** RAG + descriptive statistics. NOT a trained ML model.
> **Status:** Plan (pre-implementation), written against the real AMOS `transferWorkorder` XML.

---

## Table of contents
- [0. The one thing that matters: precursor identification](#0)
- [1. Data format (real AMOS XML)](#1)
- [2. Architecture: three layers](#2)
- [3. The precursor matcher (L0–L3) — the core design](#3)
- [4. Pipeline overview & DAG](#4)
- [5. STEP-BY-STEP INSTRUCTIONS](#5)
  - [Stage A — Infra](#A)
  - [Stage B — Ingest (XML → BQ)](#B)
  - [Step 01 — Replacement events](#s01)
  - [Step 02 — Top-3 focus components](#s02)
  - [Step 03 — Reference set](#s03)
  - [Step 04 — Embed all reference-set WOs](#s04)
  - [Step 05 — Candidate precursors (L0+L1)](#s05)
  - [Step 06 — Semantic scoring (L2)](#s06)
  - [Step 07 — LLM adjudication (L3)](#s07)
  - [Step 08 — Lead-time samples](#s08)
  - [Step 09 — Load online vector index](#s09)
  - [Step 10 — Validation & Go/No-go](#s10)
- [6. Output artifacts](#6)
- [7. Tunable parameters](#7)
- [8. Open items](#8)
- [9. Build order](#9)
- [10. Reusable repo assets](#10)

---

<a name="0"></a>
## 0. The one thing that matters: precursor identification

The entire prediction rests on one question:

> **Given a component that was later replaced, which earlier work order was the *precursor
> symptom* of that failure?**

Everything else (frequency ranking, reference set, statistics) is straightforward SQL. Precursor
identification is the hard, accuracy-defining problem. If precursors are wrong, every lead-time
number is noise dressed up with false confidence.

**Design principle — anchor-driven matching.** The *replacement* is certain (it is a serialized
part swap inside the WO). The *precursor* is uncertain. So we use the replacement as the **anchor**
and search **backwards** on the same aircraft for the WO whose text best matches the failure the
replacement fixed. This is the online RAG logic run offline and in reverse, which means our
offline validation exercises the *same* relevance mechanism the online agent will use.

We implement **four stacked filters** (detailed in §3): L0 temporal, L1 ATA pre-filter,
L2 embedding similarity, L3 LLM adjudication. Cheap filters shrink the candidate set; the
expensive LLM only judges the few survivors.

---

<a name="1"></a>
## 1. Data format (real AMOS XML)

Input = a stream of AMOS **`transferWorkorder`** XML envelopes (payload
`type="transferWorkorder" version="3.1"`). Everything needed is inside each `<workorder>`.

### 1.1 Annotated field map (from the real sample)

```
amosTransportEnvelope
└─ payload type="transferWorkorder"
   └─ transferWorkorder
      └─ workorder uuid=...                                    → wo_uuid
         ├─ workorderNumber ............... 3225485            → wo_id
         ├─ workorderHeader
         │  ├─ workorderType ............... M                 → wo_type
         │  ├─ workorderState .............. C                 → wo_state  (C = closed)
         │  ├─ aircraft
         │  │  ├─ aircraftType ............. B737-8            → ac_type
         │  │  ├─ aircraftFullRegistration . G-RUKI            → aircraft_full_reg
         │  │  ├─ aircraftRegistration ..... RUKI              → aircraft_reg   ★ JOIN KEY
         │  │  └─ aircraftMsn .............. 33587             → aircraft_msn
         │  ├─ ataChapter .................. 38-00             → ata_chapter
         │  ├─ issueData/issueDate ......... 2010-06-19Z       → issue_date
         │  └─ closingData
         │     ├─ closingDate .............. 2010-06-19Z       → close_date
         │     ├─ closingTahMinutes ........ 913860            → tah_minutes (hours×60)
         │     └─ closingTac .............. 10062             → tac  ★ CYCLE STAMP (R6)
         └─ workSteps/workStep (repeated)
            ├─ workStepSequenceNumber ...... 1                 → work_step_seq
            ├─ description .................. "AFT R/H TOILET…" → step_description  (embed text)
            └─ actions/action (repeated)
               ├─ actionText ............... "…ASSY REPLACED…"  → action_text       (embed text)
               └─ componentChanges/componentChange (repeated)  → wo_uuid child
                  ├─ position ............... CABIN             → position
                  ├─ labelNumber ........... 1107552           → label_number
                  ├─ partOff/partNumber ..... 15800-029-3       → part_off_pn
                  ├─ partOff/serialNumber ... 2624              → serial_off
                  ├─ partOn/partNumber ...... 15800-029-3       → part_on_pn
                  └─ partOn/serialNumber .... 3407              → serial_on
```

### 1.2 Confirmed decisions

| Topic | Decision |
|-------|----------|
| **Cycle stamp** | `closingData/closingTac` (Total Aircraft Cycles at close). Lead time = `replacement_tac − precursor_tac`. |
| **Ingestion** | Raw XML → GCS → Python parser → BQ `raw.work_orders` + `raw.component_changes`. |
| **Serialized part** | No part master in feed → *infer* serialized from a `componentChange` having both serials. |
| **Replacement (swap)** | A `componentChange` with `serial_off` and `serial_on` both present and **different**. |
| **Component key** | `PN | position` (installed part number + position), e.g. `15800-029-3|CABIN`. |
| **Precursor matcher** | Four layers L0–L3, all implemented for MVP (§3). |
| **Embedding coverage** | Embed **all WOs on reference-set aircraft**. |

### 1.3 Fields NOT in the feed (become heuristics / limitations)

- No **removal-reason code** → cannot code-exclude cannibalization or scheduled removals; MVP
  keeps all swaps (requirements §8.4); L3 LLM handles routine/cannibalization exclusion via text.
- No **part master** (`is_serialized`/`is_rotable`/`superseded_by`) → serialized inferred; SB
  supersession not collapsed (flagged when `part_off_pn ≠ part_on_pn`).
- No **component TSN/CSN/TSO** → lead time is in **airframe cycles** (`closingTac`), not component
  life. Accepted for MVP; documented limitation.

---

<a name="2"></a>
## 2. Architecture: three layers

| Layer | Owns | Tool | Lifecycle |
|-------|------|------|-----------|
| **Infra** (`infra/`) | Resource *existence*: APIs, datasets, service account/IAM, GCS buckets (raw-xml + vector-search), BQ↔Vertex connection, Vector Search index + endpoint, scheduler. | Terraform OR Python (both scaffolded) | Provisioned once |
| **Ingest** (`ingest/`) | XML → BQ. Faithful flatten of `transferWorkorder` into `raw.work_orders` + `raw.component_changes`. No business logic. | Python (`lxml` + BigQuery client) | Every new batch of XML |
| **Transforms** (`transforms/`) | Data logic: replacement events → Top-3 → reference set → embeddings → precursor matching (L0–L3) → lead-time stats → index load → validation. | SQL + Python | Repeated on cadence |

**BQ table ownership boundary:** infra owns **datasets only**. `raw.*` written by ingest.
`curated.*` are `CREATE OR REPLACE TABLE AS SELECT` outputs of transforms. Terraform never
declares a table the pipeline rewrites.

```
repo/
├── infra/            terraform/ (main,bigquery,iam,storage,vertex,scheduler,variables,outputs)
│                     python/    (config.py, provision.py, requirements.txt)
├── ingest/           parse_workorders.py, load_raw.py,
│                     schema_work_orders.json, schema_component_changes.json, requirements.txt
└── transforms/       01_replacement_events.sql
                      02_top3_focus_components.sql
                      03_reference_set.sql
                      04_embed_reference_wos.sql
                      05_candidate_precursors.sql
                      06_semantic_scoring.sql
                      07_llm_adjudication.sql
                      08_lead_time_samples.sql
                      09_load_vector_index.py
                      10_validation.sql
```

---

<a name="3"></a>
## 3. The precursor matcher (L0–L3) — the core design

For each **replacement event R** (certain), find the earlier WO(s) on the **same aircraft** that
are genuine **precursor symptoms** of that failure. Four stacked filters, cheap → expensive:

| Layer | Filter | Signal | Implemented in | Purpose |
|-------|--------|--------|----------------|---------|
| **L0** | same aircraft AND `precursor.tac < R.tac` | SQL | Step 05 | Hard prerequisite (lead must be positive). |
| **L1** | ATA-4 prefix match (`38-32`, not just `38`) | SQL | Step 05 | Coarse pre-filter to shrink candidates before scoring. |
| **L2** | cosine similarity(precursor.embedding, R.anchor_embedding) ≥ `SIM_THRESHOLD` | vector | Step 06 | The real relevance signal — matches the *language of the failure*. |
| **L3** | LLM verdict ∈ {`symptom`} (rejects `routine`, `unrelated`, `cannibalization`) | LLM | Step 07 | Precision + causality; auditable rationale. |

**Anchor text for R (the query).** Build R's anchor from the replacement WO's own text:
`step_description` + `action_text` of the workStep that contained the swap (e.g.
*"AFT R/H TOILET CONSTANTLY DRIPPING… / RINSE VALVE U/S / AFT R/H TOILET ASSY REPLACED"*). This
text describes the component and its failure mode better than any code.

**Why anchor-driven:** the replacement is the only certain thing we have. Searching backwards from
it, by *meaning*, is the most defensible way to identify a precursor without ground-truth labels.
It also mirrors the online path (new WO → similar precursors), so validation is representative.

**Cost control:** L0+L1 run in SQL over everything and cut candidates to a handful per
replacement. L2 scores only those. L3 (LLM) judges only the L2 survivors (top few per
replacement) — affordable even at 100k+ WOs because it is never run on all pairs.

---

<a name="4"></a>
## 4. Pipeline overview & DAG

```
INFRA ─► INGEST(XML→raw) ─► 01 replacement_events ─► 02 top3 ─► 03 reference_set
                                                                      │
                                                                      ▼
                                                  04 embed reference-set WOs
                                                                      │
   05 candidate_precursors (L0+L1) ◄────────────────────────────────┘
             │
             ▼
   06 semantic_scoring (L2) ─► 07 llm_adjudication (L3) ─► 08 lead_time_samples
                                                                      │
                                          09 load_vector_index ◄──────┤ (uses 04 embeddings)
                                                                      ▼
                                                              10 validation → Go/No-go
```

Order: infra → ingest → 01 → 02 → 03 → 04 → 05 → 06 → 07 → 08 → (09 ‖ 10).

---

<a name="5"></a>
## 5. STEP-BY-STEP INSTRUCTIONS

Each step below states: **Goal · Inputs · Instructions · Output · Done-when**.

---

<a name="A"></a>
### Stage A — Infra (`infra/`)

**Goal.** Make every resource the pipeline needs exist. No data logic.

**Instructions.**
1. Pick one approach for this environment (Terraform *or* Python) — do not run both against the
   same project.
2. Provision, in this order:
   1. Enable APIs: `bigquery`, `aiplatform`, `storage`.
   2. BQ datasets: `raw`, `curated`.
   3. GCS buckets: **`<proj>-pma-raw-xml`** (ingest input) and **`<proj>-pma-vs`** (vector-search JSONL).
   4. Service account `pma-poc-pipeline` + roles: `bigquery.jobUser`, `bigquery.dataEditor`,
      `aiplatform.user`, `storage.objectAdmin`.
   5. BQ↔Vertex connection `vertex_conn`; grant its service agent `aiplatform.user`.
   6. Vector Search index + endpoint + deployed index (`pma_poc_wo_v1`), dims = `EMBEDDING_DIM`,
      distance = `COSINE_DISTANCE`, `BATCH_UPDATE`.
   7. (Last, deferred) Cloud Scheduler job for periodic rebuild.
3. Export outputs consumed downstream: dataset ids, SA email, both bucket names, connection id
   (`<location>.vertex_conn`), index id, endpoint id.

**Output.** All resources exist; `infra` outputs available to ingest + transforms.
**Done-when.** `terraform apply` (or `provision.py --all`) is clean and idempotent on re-run.

> Provision only steps 1–4 first; defer 5–6 (the slow/costly Vertex resources) until Step 08
> confirms a real signal (see §9 build order).

---

<a name="B"></a>
### Stage B — Ingest: XML → BQ (`ingest/`)

**Goal.** Faithfully flatten `transferWorkorder` XML into two BQ tables. Getting the unnesting
right is the first correctness gate.

**Inputs.** Raw XML files in `gs://<proj>-pma-raw-xml/…`.

**Instructions (`parse_workorders.py`).**
1. Stream each XML file from GCS. Support **many `<workorder>` per file** and many files.
2. **Namespaces:** the envelope declares `dsig`; `transferWorkorder`/`workorder` are in the
   default namespace. Strip/ignore namespaces when matching element paths.
3. **Unnest fully:** one WO → many `workStep` → many `action` → many `componentChange`. Emit:
   - **`raw.work_orders`** — one row per WO (dedupe on `wo_uuid`), fields per §1.1 header map,
     plus `description_concat` = all `workStep/description` + `action/actionText`, in step order,
     newline-joined; plus `source_file`, `ingested_at`.
   - **`raw.component_changes`** — one row per `componentChange` (dedupe on `cc_uuid`), with
     `wo_uuid`, `wo_id`, `aircraft_reg`, `tac`, `close_date`, `work_step_seq`, `position`,
     `label_number`, `part_off_pn`, `serial_off`, `part_on_pn`, `serial_on`, `action_text`,
     `step_description`.
4. **Normalize:** trim+uppercase `part_*_pn`, `serial_*`, `aircraft_reg`, `position`; parse
   `2010-06-19Z` → `DATE`; cast `tac`/`tah_minutes` → `INT64` (null-safe).
5. **Missing nodes:** a WO with no `componentChange` still yields a `work_orders` row and **zero**
   `component_changes` rows.
6. **Load (`load_raw.py`):** write NDJSON, then load into BQ with explicit schemas
   (`schema_*.json`). MVP: truncate-and-load on a sample. Incremental later: `MERGE` on
   `wo_uuid` / `cc_uuid`.

**Output.** `raw.work_orders`, `raw.component_changes`.
**Done-when.** Row counts and a hand-check of a multi-step / multi-componentChange WO match the
source XML; `description_concat` for the sample reads as the four expected lines.

---

<a name="s01"></a>
### Step 01 — Replacement events (`01_replacement_events.sql`)

**Goal.** The certain anchors: true serialized swaps.
**Inputs.** `raw.component_changes`, `raw.work_orders`.

**Instructions.** Keep a `componentChange` only when both serials exist and differ. Key the
component as `PN|position`. Attach the **anchor text** from the parent workStep for later scoring.

```sql
CREATE OR REPLACE TABLE curated.fct_replacement_events AS
SELECT
  cc.aircraft_reg,
  CONCAT(UPPER(TRIM(COALESCE(cc.part_on_pn, cc.part_off_pn))), '|', UPPER(TRIM(cc.position))) AS component_key,
  cc.part_off_pn, cc.part_on_pn, cc.position, cc.label_number,
  cc.serial_off, cc.serial_on,
  cc.wo_id      AS replacement_wo_id,
  cc.wo_uuid    AS replacement_wo_uuid,
  cc.tac        AS replacement_tac,
  cc.close_date AS replacement_date,
  -- anchor text = the failure language of THIS replacement (query for L2/L3)
  CONCAT(COALESCE(cc.step_description,''), '\n', COALESCE(cc.action_text,'')) AS anchor_text,
  (cc.part_off_pn <> cc.part_on_pn) AS is_supersession   -- SB/mod flag (§1.3)
FROM raw.component_changes cc
WHERE cc.serial_off IS NOT NULL AND TRIM(cc.serial_off) <> ''
  AND cc.serial_on  IS NOT NULL AND TRIM(cc.serial_on)  <> ''
  AND UPPER(TRIM(cc.serial_off)) <> UPPER(TRIM(cc.serial_on))   -- exclude same-serial reinstall/overhaul
  AND cc.tac IS NOT NULL;                                       -- need a cycle stamp
```

**Output.** `curated.fct_replacement_events`.
**Done-when.** Step 10 check 2 (definition spot-check) passes on a sample.

---

<a name="s02"></a>
### Step 02 — Top-3 focus components (`02_top3_focus_components.sql`)

**Goal.** `TOP_3` most-replaced components (online Step-1 gate).
**Inputs.** `curated.fct_replacement_events`.

**Instructions.** Rank components by replacement count; keep top 3. Also emit the **full** ranked
list (view) for stakeholder review — needed to confirm each Top-3 has enough precursor history.

```sql
CREATE OR REPLACE TABLE curated.dim_focus_components AS
WITH freq AS (
  SELECT component_key,
         COUNT(*)                     AS replacement_count,
         COUNT(DISTINCT aircraft_reg) AS aircraft_with_replacement
  FROM curated.fct_replacement_events
  GROUP BY component_key
)
SELECT component_key, replacement_count, aircraft_with_replacement,
       RANK() OVER (ORDER BY replacement_count DESC) AS freq_rank
FROM freq
QUALIFY freq_rank <= 3;

CREATE OR REPLACE VIEW curated.v_component_frequency AS
SELECT component_key, COUNT(*) AS replacement_count,
       COUNT(DISTINCT aircraft_reg) AS aircraft_with_replacement
FROM curated.fct_replacement_events
GROUP BY component_key
ORDER BY replacement_count DESC;
```

**Output.** `curated.dim_focus_components` (+ `v_component_frequency`).
**Done-when.** The three keys look sane to a domain reviewer and each has multiple aircraft.

---

<a name="s03"></a>
### Step 03 — Reference set (`03_reference_set.sql`)

**Goal.** R3: aircraft that replaced each focus component, with replacement TAC.
**Inputs.** `fct_replacement_events`, `dim_focus_components`.

```sql
CREATE OR REPLACE TABLE curated.dim_reference_set AS
SELECT e.component_key, e.aircraft_reg,
       e.replacement_wo_id, e.replacement_wo_uuid,
       e.replacement_tac, e.replacement_date, e.anchor_text
FROM curated.fct_replacement_events e
JOIN curated.dim_focus_components f USING (component_key);
```

Keep **all** replacements per aircraft (an aircraft may replace a component more than once).
**Output.** `curated.dim_reference_set`.
**Done-when.** Every focus component has ≥1 reference aircraft; counts match Step 02.

---

<a name="s04"></a>
### Step 04 — Embed all reference-set WOs (`04_embed_reference_wos.sql`)

**Goal.** Embeddings for **every WO on reference-set aircraft** (coverage for both offline
matching and online retrieval). Also embed each replacement's **anchor text**.
**Inputs.** `raw.work_orders`, `dim_reference_set`, infra connection.

**Instructions.**
1. Create the remote embedding model bound to the infra connection.
2. Embed the `description_concat` of all WOs whose `aircraft_reg` is in the reference set.
3. Embed the replacement anchors (small table) for use as L2 query vectors.

```sql
CREATE OR REPLACE MODEL curated.emb_model
REMOTE WITH CONNECTION `${location}.vertex_conn`
OPTIONS (ENDPOINT = 'text-embedding-005');   -- confirm model + EMBEDDING_DIM (§7)

-- 4a. embeddings for all WOs on reference-set aircraft
CREATE OR REPLACE TABLE curated.wo_embeddings AS
SELECT wo_uuid, wo_id, aircraft_reg, ata_chapter, tac,
       content, ml_generate_embedding_result AS embedding
FROM ML.GENERATE_EMBEDDING(
  MODEL curated.emb_model,
  (
    SELECT w.wo_uuid, w.wo_id, w.aircraft_reg, w.ata_chapter, w.tac,
           w.description_concat AS content
    FROM raw.work_orders w
    WHERE w.aircraft_reg IN (SELECT DISTINCT aircraft_reg FROM curated.dim_reference_set)
      AND w.description_concat IS NOT NULL AND TRIM(w.description_concat) <> ''
  )
);

-- 4b. anchor embeddings (one per replacement event)
CREATE OR REPLACE TABLE curated.replacement_anchor_embeddings AS
SELECT replacement_wo_uuid, component_key, aircraft_reg, replacement_tac,
       content, ml_generate_embedding_result AS anchor_embedding
FROM ML.GENERATE_EMBEDDING(
  MODEL curated.emb_model,
  (
    SELECT replacement_wo_uuid, component_key, aircraft_reg, replacement_tac,
           anchor_text AS content
    FROM curated.dim_reference_set
    WHERE anchor_text IS NOT NULL AND TRIM(anchor_text) <> ''
  )
);
```

**Output.** `curated.wo_embeddings`, `curated.replacement_anchor_embeddings`.
**Done-when.** Row counts ≈ eligible WOs; embedding vectors non-null; dim = `EMBEDDING_DIM`.

---

<a name="s05"></a>
### Step 05 — Candidate precursors: L0 + L1 (`05_candidate_precursors.sql`)

**Goal.** Cheap SQL pre-filter: same aircraft, earlier cycle, same ATA-4 area.
**Inputs.** `dim_reference_set`, `raw.work_orders`.

**Instructions.** For each replacement R, take every earlier WO on the same aircraft (L0) whose
ATA-4 prefix matches R's (L1). ATA-4 = first two groups, e.g. `38-32`.

```sql
CREATE OR REPLACE TABLE curated.candidate_precursors AS
WITH repl AS (
  SELECT r.component_key, r.aircraft_reg,
         r.replacement_wo_uuid, r.replacement_tac,
         w.ata_chapter AS repl_ata
  FROM curated.dim_reference_set r
  JOIN raw.work_orders w ON w.wo_uuid = r.replacement_wo_uuid
),
ata4 AS (   -- normalize ATA to 4-char area, e.g. '38-32'
  SELECT *, REGEXP_EXTRACT(repl_ata, r'^([0-9]{2}-[0-9]{2})') AS repl_ata4 FROM repl
)
SELECT
  a.component_key, a.aircraft_reg,
  a.replacement_wo_uuid, a.replacement_tac,
  w.wo_uuid AS precursor_wo_uuid, w.wo_id AS precursor_wo_id, w.tac AS precursor_tac
FROM ata4 a
JOIN raw.work_orders w
  ON w.aircraft_reg = a.aircraft_reg
 AND w.tac < a.replacement_tac                                   -- L0 temporal
 AND w.wo_uuid <> a.replacement_wo_uuid
 AND REGEXP_EXTRACT(w.ata_chapter, r'^([0-9]{2}-[0-9]{2})') = a.repl_ata4;  -- L1 ATA-4
```

**Output.** `curated.candidate_precursors` (many per replacement — that is expected).
**Done-when.** Non-empty for the Top-3; candidate counts are plausible (tens, not tens of
thousands, per replacement). If L1 is too tight, relax to ATA-2 and note it in §8.

---

<a name="s06"></a>
### Step 06 — Semantic scoring: L2 (`06_semantic_scoring.sql`)

**Goal.** Keep only candidates whose text is semantically close to the replacement's failure
language. This is the real relevance filter.
**Inputs.** `candidate_precursors`, `wo_embeddings`, `replacement_anchor_embeddings`.

**Instructions.** Cosine-score each candidate's embedding against its replacement's anchor
embedding; keep those ≥ `SIM_THRESHOLD`; keep the top `K_PRECURSORS` per replacement.

```sql
CREATE OR REPLACE TABLE curated.scored_precursors AS
WITH scored AS (
  SELECT
    c.component_key, c.aircraft_reg,
    c.replacement_wo_uuid, c.replacement_tac,
    c.precursor_wo_uuid, c.precursor_wo_id, c.precursor_tac,
    -- cosine similarity = 1 - cosine distance
    (1 - ML.DISTANCE(pe.embedding, ae.anchor_embedding, 'COSINE')) AS sim
  FROM curated.candidate_precursors c
  JOIN curated.wo_embeddings pe                ON pe.wo_uuid = c.precursor_wo_uuid
  JOIN curated.replacement_anchor_embeddings ae ON ae.replacement_wo_uuid = c.replacement_wo_uuid
)
SELECT * EXCEPT(rn) FROM (
  SELECT s.*,
         ROW_NUMBER() OVER (PARTITION BY replacement_wo_uuid ORDER BY sim DESC) AS rn
  FROM scored s
  WHERE sim >= @SIM_THRESHOLD              -- L2 threshold (§7)
)
WHERE rn <= @K_PRECURSORS;                 -- top-K per replacement (§7)
```

**Output.** `curated.scored_precursors`.
**Done-when.** Step 10 check 4 (manual audit of ~50 pairs) shows an acceptable false-positive
rate; tune `SIM_THRESHOLD` / `K_PRECURSORS` from that audit.

---

<a name="s07"></a>
### Step 07 — LLM adjudication: L3 (`07_llm_adjudication.sql`)

**Goal.** Precision + causality. Reject pairs where the earlier WO is routine work, cannibalization,
a duplicate/near-replacement, or coincidental text — none of which the feed's codes can catch.
**Inputs.** `scored_precursors`, `raw.work_orders` (texts), infra connection (Gemini).

**Instructions.**
1. For each surviving pair, send the **precursor text** and the **replacement anchor text** to a
   Vertex LLM via `ML.GENERATE_TEXT` (bound to the same connection).
2. Prompt the model to classify the relationship and return **strict JSON**.
3. Keep only `verdict = "symptom"`.

**Prompt contract (JSON output):**
```json
{
  "verdict": "symptom | routine | cannibalization | unrelated | duplicate",
  "confidence": 0.0,
  "reason": "one sentence citing the shared failure mode or why rejected"
}
```
**Prompt skeleton:**
```
You are an aircraft maintenance analyst. A component was REPLACED (the anchor).
Decide whether the EARLIER work order describes an emerging symptom/defect that
plausibly PRECEDED and relates to that replacement.
- "symptom": earlier WO reports a degrading defect on the same component/system.
- "routine": scheduled check/inspection/servicing, not a failure symptom.
- "cannibalization": part removed to serve another aircraft.
- "duplicate": same event / the replacement itself restated.
- "unrelated": different system or coincidental text match.
Return ONLY the JSON object.

REPLACEMENT (anchor): <<<{anchor_text}>>>
EARLIER WORK ORDER:  <<<{precursor_text}>>>
```

```sql
CREATE OR REPLACE TABLE curated.adjudicated_precursors AS
WITH pairs AS (
  SELECT sp.*, wp.description_concat AS precursor_text, ae.content AS anchor_text
  FROM curated.scored_precursors sp
  JOIN raw.work_orders wp ON wp.wo_uuid = sp.precursor_wo_uuid
  JOIN curated.replacement_anchor_embeddings ae ON ae.replacement_wo_uuid = sp.replacement_wo_uuid
),
judged AS (
  SELECT p.*,
         JSON_VALUE(ml_generate_text_result, '$.verdict')            AS verdict,
         SAFE_CAST(JSON_VALUE(ml_generate_text_result,'$.confidence') AS FLOAT64) AS llm_conf,
         JSON_VALUE(ml_generate_text_result, '$.reason')             AS reason
  FROM ML.GENERATE_TEXT(
         MODEL curated.llm_model,     -- Gemini model bound to vertex_conn (create like emb_model)
         (SELECT *, <<prompt built from anchor_text + precursor_text>> AS prompt FROM pairs),
         STRUCT(0.0 AS temperature, TRUE AS flatten_json_output)
       )
)
SELECT * FROM judged WHERE verdict = 'symptom';
```

**Output.** `curated.adjudicated_precursors` (verified precursor→replacement pairs + rationale).
**Done-when.** Spot-check confirms verdicts are sensible; rejection reasons are legible (feeds the
auditability NFR and the online gate prompt reuse).

> **Reuse:** this same prompt/schema is the online Step-1 component-gate. Building it here delivers
> that open deliverable too.

---

<a name="s08"></a>
### Step 08 — Lead-time samples (`08_lead_time_samples.sql`)

**Goal.** The distribution inputs. For each verified precursor, lead = replacement TAC − precursor
TAC; keep the **first** qualifying replacement after the precursor.
**Inputs.** `curated.adjudicated_precursors`.

```sql
CREATE OR REPLACE TABLE curated.fct_lead_time_samples AS
SELECT * EXCEPT(rn) FROM (
  SELECT
    a.component_key, a.aircraft_reg,
    a.precursor_wo_id, a.precursor_wo_uuid, a.precursor_tac,
    a.replacement_wo_uuid, a.replacement_tac,
    (a.replacement_tac - a.precursor_tac) AS lead_cycles,
    a.sim, a.verdict, a.llm_conf,
    ROW_NUMBER() OVER (
      PARTITION BY a.component_key, a.aircraft_reg, a.precursor_wo_uuid
      ORDER BY a.replacement_tac ASC
    ) AS rn
  FROM curated.adjudicated_precursors a
  WHERE a.replacement_tac > a.precursor_tac                 -- directionality guard (lead > 0)
    -- AND (a.replacement_tac - a.precursor_tac) <= @REPLACEMENT_WINDOW   -- optional cap (§7)
)
WHERE rn = 1;
```

**Output.** `curated.fct_lead_time_samples`.
**Done-when.** All `lead_cycles > 0`; distribution preview (Step 10) is inspected.

---

<a name="s09"></a>
### Step 09 — Load online vector index (`09_load_vector_index.py`)

**Goal.** Populate the infra-provisioned Vector Search index for the **online** retrieval path.
**Inputs.** `curated.wo_embeddings`, infra index/endpoint/bucket.

**Instructions.**
1. Export embeddings to Vector Search JSONL in the VS bucket, with restricts for online filtering.
2. Batch-update the existing index; smoke-test the endpoint.

```python
rows = bq.query("""
  SELECT wo_uuid AS id, embedding, aircraft_reg, ata_chapter, tac
  FROM curated.wo_embeddings
""")
write_jsonl(rows, gcs_uri=f"gs://{VS_BUCKET}/index/wo.json",
            fields={"id": "wo_uuid", "embedding": "embedding",
                    "restricts": [{"namespace": "aircraft_reg", "allow": ["<aircraft_reg>"]}],
                    "numeric_restricts": [{"namespace": "tac", "value_int": "<tac>"}]})
MatchingEngineIndex(INDEX_ID).update_embeddings(contents_delta_uri=f"gs://{VS_BUCKET}/index/")
MatchingEngineIndexEndpoint(ENDPOINT_ID).find_neighbors(
    deployed_index_id="pma_poc_wo_v1", queries=[sample_vector], num_neighbors=10)
```
> Restrict namespaces support online Step 3 (filter to reference-set aircraft; use `tac` to keep
> only WOs earlier than the new WO if desired). Component filtering is applied via the reference
> set join, since `component_key` is precursor-relationship-derived, not intrinsic to a WO.

**Output.** Populated, queryable index.
**Done-when.** `find_neighbors` returns sensible neighbours for a known symptom vector.

---

<a name="s10"></a>
### Step 10 — Validation & Go/No-go (`10_validation.sql`)

**Goal.** Prove the artifacts before building the online agent.
**Inputs.** all `curated.*`.

**Checks.**
1. **Parse fidelity** — WO & componentChange counts vs source; % WOs with ≥1 swap; % changes
   missing serial/TAC.
2. **Replacement definition** — sample swaps; confirm `serial_off≠serial_on`, genuine part change.
3. **Cycle sanity** — `tac>0`, monotonic within an aircraft; swaps dropped for missing TAC.
4. **Precursor false-positive audit** — sample 50 pairs from `scored_precursors` (pre-L3) and from
   `adjudicated_precursors` (post-L3); human-label plausible/not; report FP rate before vs after
   L3. This quantifies the value of the LLM layer and tunes `SIM_THRESHOLD`.
5. **Distribution preview** — the "is the signal real?" gate:
```sql
SELECT component_key,
       COUNT(*)                                           AS n,
       AVG(lead_cycles)                                   AS mean_lead,
       APPROX_QUANTILES(lead_cycles,100)[OFFSET(50)]      AS p50,
       APPROX_QUANTILES(lead_cycles,100)[OFFSET(90)]      AS p90,
       STDDEV(lead_cycles)                                AS stdev,
       SAFE_DIVIDE(STDDEV(lead_cycles), AVG(lead_cycles)) AS cv
FROM curated.fct_lead_time_samples
GROUP BY component_key ORDER BY n DESC;
```
6. **Coverage** — components/aircraft with usable samples; funnel counts L0→L1→L2→L3.

**Output.** Validation report.
**Done-when.** Reviewer signs Go/No-go for the online vertical slice, and `MIN_SAMPLE`/`MAX_CV`/
`SIM_THRESHOLD` are set from the evidence.

---

<a name="6"></a>
## 6. Output artifacts (consumed by the online agent)

| Artifact | Step | Consumed by (online §7) |
|----------|------|-------------------------|
| `raw.work_orders`, `raw.component_changes` | Ingest | source of everything |
| `curated.fct_replacement_events` | 01 | Step 4 lookup + audit |
| `curated.dim_focus_components` | 02 | Step 1 gating (`TOP_3`) |
| `curated.dim_reference_set` | 03 | Step 3 RAG filter |
| `curated.wo_embeddings` | 04 | Step 3 retrieval + online index |
| `curated.replacement_anchor_embeddings` | 04 | offline matching |
| `curated.scored_precursors` / `adjudicated_precursors` | 06 / 07 | precursor provenance + audit |
| `curated.fct_lead_time_samples` | 08 | Steps 4–5 statistics |
| Vector Search index + endpoint | infra + 09 | Step 3 retrieval |
| Validation report | 10 | Go/No-go |

---

<a name="7"></a>
## 7. Tunable parameters

| Param | Meaning | Set in | Initial |
|-------|---------|--------|---------|
| `EMBEDDING_DIM` | embedding output dim | infra + Step 04 | model default (confirm) |
| `SIM_THRESHOLD` | min cosine sim for L2 | Step 06 | 0.7 (tune via Step 10 check 4) |
| `K_PRECURSORS` | max precursors per replacement | Step 06 | 5 |
| `REPLACEMENT_WINDOW` | max cycles precursor→replacement | Step 08 | none (optional cap) |
| `MIN_SAMPLE` | min samples for OK confidence | online §7 | 8 |
| `MAX_CV` | max stdev/mean for OK confidence | online §7 | 0.5 |
| ATA prefix width | L1 granularity (ATA-4 vs ATA-2) | Step 05 | ATA-4 |

---

<a name="8"></a>
## 8. Open items

Resolved this session:
- [x] Detection method — Option A, signal is in-WO (`componentChange`).
- [x] Cycle stamp — `closingTac`.
- [x] Ingestion — XML→GCS→Python→BQ.
- [x] Serialized inference — from serial presence.
- [x] Precursor identification approach — anchor-driven L0–L3.

Still open:
- [ ] **Cannibalization/duplicate handling** — relies on L3 text verdict (no code in feed); audit.
- [ ] **SB/mod supersession** — `is_supersession` flagged; decide whether to collapse keys (needs map).
- [ ] **ATA granularity** — ATA-4 vs ATA-2 for L1 (Step 05); driven by candidate volume + FP rate.
- [ ] **`SIM_THRESHOLD` / `K_PRECURSORS`** — from Step 10 check 4.
- [ ] **Embedding + LLM models** — confirm `text-embedding-005` dim and Gemini model id.
- [ ] **Component-life cycles** — feed only has airframe TAC; documented limitation.
- [ ] **Replacement-window cap** — Step 08.
- [ ] **Provisioning tool** — Terraform vs Python.
- [ ] **Ingest idempotency** — truncate-load vs MERGE-on-uuid.
- [ ] **Refresh cadence** — scheduler (infra).
- [ ] **TF state backend** — only if Terraform.

---

<a name="9"></a>
## 9. Build order (thin, testable increments)

1. **Infra bootstrap (partial):** APIs + datasets + SA/IAM + raw-xml bucket.
2. **Ingest on a SAMPLE** → `raw.*`; hand-verify unnesting. *(first correctness gate)*
3. **Step 01** replacement events → **Step 10 checks 1–3** early.
4. **Steps 02–03** Top-3 + reference set → review full frequency list.
5. **Infra (rest):** connection + Step 04 embeddings (needs connection). Embed reference-set WOs.
6. **Steps 05–06** L0+L1 candidates → L2 scoring → **Step 10 check 4** (FP audit; tune threshold).
7. **Step 07** L3 LLM adjudication → re-run check 4; compare FP before/after L3.
8. **Step 08** lead-time samples → **Step 10 check 5** distribution preview (is the signal real?).
9. **Infra:** Vector Search index/endpoint → **Step 09** load + smoke test.
10. **Full Step 10** report → **Go/No-go**. Scheduler + incremental MERGE last.

> Rationale: the two make-or-break gates are **XML parsing** (step 2) and the **precursor
> false-positive audit** (step 6–7). Prove both on a sample before paying for the Vertex index.

---

<a name="10"></a>
## 10. Reusable repo assets
- **BigQuery tool / SQL patterns:** `asl_genai/scaffolds/adk_ide/agent_15_big_query_tool`, `agent_27_mcp_bigquery`.
- **RAG / embeddings + vector search:** `asl_genai/notebooks/retrieval_augmented_generation/`.
- **Stats / time-series reference:** `asl_core/notebooks/time_series_prediction/`.
- **MLOps / pipeline orchestration:** `asl_mlops/notebooks/`.
