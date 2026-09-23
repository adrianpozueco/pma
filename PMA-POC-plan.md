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
- [11. Final PMA online agent plan (production inference)](#11)

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
**Inputs.** `pma_agent_analytics.wo_workorders`.

**Instructions.** Unnest `work_steps -> actions -> component_changes`. Keep a `component_change`
only when both serials exist and differ. Key the component as `PN|position`. Attach the
**anchor text** from the parent work step/action for later scoring.

```sql
CREATE OR REPLACE TABLE curated.fct_replacement_events AS
SELECT
  wo.aircraft.full_registration AS aircraft_reg,
  CONCAT(
    UPPER(TRIM(COALESCE(cc.part_on_number, cc.part_off_number))),
    '|',
    UPPER(TRIM(cc.position))
  ) AS component_key,
  cc.part_off_number AS part_off_pn,
  cc.part_on_number AS part_on_pn,
  cc.position,
  cc.label_number,
  cc.part_off_serial AS serial_off,
  cc.part_on_serial AS serial_on,
  wo.workorder_number AS replacement_wo_id,
  wo.workorder_uuid AS replacement_wo_uuid,
  wo.closing.total_aircraft_cycles AS replacement_tac,
  wo.closing.date AS replacement_date,
  -- anchor text = the failure language of THIS replacement (query for L2/L3)
  CONCAT(COALESCE(ws.description, ''), '\n', COALESCE(a.action_text, '')) AS anchor_text,
  (cc.part_off_number <> cc.part_on_number) AS is_supersession   -- SB/mod flag (§1.3)
FROM `pma_agent_analytics.wo_workorders` AS wo
CROSS JOIN UNNEST(IFNULL(wo.work_steps, [])) AS ws
CROSS JOIN UNNEST(IFNULL(ws.actions, [])) AS a
CROSS JOIN UNNEST(IFNULL(a.component_changes, [])) AS cc
WHERE NULLIF(TRIM(cc.part_off_serial), '') IS NOT NULL
  AND NULLIF(TRIM(cc.part_on_serial), '') IS NOT NULL
  AND UPPER(TRIM(cc.part_off_serial)) <> UPPER(TRIM(cc.part_on_serial))
  AND wo.closing.total_aircraft_cycles IS NOT NULL;
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
**Inputs.** `pma_agent_analytics.wo_workorders`, `dim_reference_set`.

**Instructions.**
1. Build WO content from `remarks + workStep.description + action.action_text`.
2. Embed all WOs whose `aircraft_reg` is in the reference set using `AI.EMBED`.
3. Embed replacement anchors (small table) for use as L2 query vectors.

```sql
-- 4a. embeddings for all WOs on reference-set aircraft
CREATE OR REPLACE TABLE curated.wo_embeddings AS
WITH work_orders_normalized AS (
  SELECT
    w.workorder_uuid AS wo_uuid,
    w.workorder_number AS wo_id,
    w.aircraft.full_registration AS aircraft_reg,
    w.ata_chapter,
    w.closing.total_aircraft_cycles AS tac,
    TRIM(
      CONCAT(
        COALESCE(w.remarks, ''),
        CASE WHEN COALESCE(w.remarks, '') <> '' THEN '\n' ELSE '' END,
        COALESCE(
          (
            SELECT ARRAY_TO_STRING(
              ARRAY(
                SELECT TRIM(
                  CONCAT(
                    COALESCE(ws.description, ''),
                    CASE
                      WHEN TRIM(COALESCE(a.action_text, '')) <> '' THEN CONCAT('\n', a.action_text)
                      ELSE ''
                    END
                  )
                )
                FROM UNNEST(IFNULL(w.work_steps, [])) AS ws
                LEFT JOIN UNNEST(IFNULL(ws.actions, [])) AS a
                WHERE TRIM(CONCAT(COALESCE(ws.description, ''), COALESCE(a.action_text, ''))) <> ''
              ),
              '\n\n'
            )
          ),
          ''
        )
      )
    ) AS content
  FROM `pma_agent_analytics.wo_workorders` AS w
),
candidate_rows AS (
  SELECT
    wo_uuid, wo_id, aircraft_reg, ata_chapter, tac, content
  FROM work_orders_normalized
  WHERE aircraft_reg IN (
    SELECT DISTINCT aircraft_reg
    FROM `curated.dim_reference_set`
  )
    AND content IS NOT NULL
    AND TRIM(content) <> ''
)
SELECT
  wo_uuid,
  wo_id,
  aircraft_reg,
  ata_chapter,
  tac,
  content,
  AI.EMBED(content, endpoint => 'text-embedding-005') AS embedding
FROM candidate_rows;

-- 4b. anchor embeddings (one per replacement event)
CREATE OR REPLACE TABLE curated.replacement_anchor_embeddings AS
WITH anchor_rows AS (
  SELECT
    replacement_wo_uuid,
    component_key,
    aircraft_reg,
    replacement_tac,
    anchor_text AS content
  FROM `curated.dim_reference_set`
  WHERE anchor_text IS NOT NULL
    AND TRIM(anchor_text) <> ''
)
SELECT
  replacement_wo_uuid,
  component_key,
  aircraft_reg,
  replacement_tac,
  content,
  AI.EMBED(content, endpoint => 'text-embedding-005') AS anchor_embedding
FROM anchor_rows;
```

**Output.** `curated.wo_embeddings`, `curated.replacement_anchor_embeddings`.
**Done-when.** Row counts ≈ eligible WOs; embedding vectors non-null; dim = `EMBEDDING_DIM`.

---

<a name="s05"></a>
### Step 05 — Candidate precursors: L0 + L1 (`05_candidate_precursors.sql`)

**Goal.** Cheap SQL pre-filter: same aircraft, earlier cycle, same ATA-4 area.
**Inputs.** `dim_reference_set`, `pma_agent_analytics.wo_workorders`.

**Instructions.** For each replacement R, take every earlier WO on the same aircraft (L0) whose
ATA-4 prefix matches R's (L1). ATA-4 = first two groups, e.g. `38-32`.

```sql
CREATE OR REPLACE TABLE curated.candidate_precursors AS
WITH work_orders AS (
  SELECT
    workorder_uuid AS wo_uuid,
    workorder_number AS wo_id,
    aircraft.full_registration AS aircraft_reg,
    ata_chapter,
    closing.total_aircraft_cycles AS tac
  FROM pma_agent_analytics.wo_workorders
),
repl AS (
  SELECT r.component_key, r.aircraft_reg,
         r.replacement_wo_uuid, r.replacement_tac,
         w.ata_chapter AS repl_ata
  FROM curated.dim_reference_set r
  JOIN work_orders w ON w.wo_uuid = r.replacement_wo_uuid
),
ata4 AS (   -- normalize ATA to 4-char area, e.g. '38-32'
  SELECT *, REGEXP_EXTRACT(repl_ata, r'^([0-9]{2}-[0-9]{2})') AS repl_ata4 FROM repl
)
SELECT
  a.component_key, a.aircraft_reg,
  a.replacement_wo_uuid, a.replacement_tac,
  w.wo_uuid AS precursor_wo_uuid, w.wo_id AS precursor_wo_id, w.tac AS precursor_tac
FROM ata4 a
JOIN work_orders w
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
    (1 - ML.DISTANCE(pe.embedding.result, ae.anchor_embedding.result, 'COSINE')) AS sim
  FROM curated.candidate_precursors c
  JOIN curated.wo_embeddings pe                ON pe.wo_uuid = c.precursor_wo_uuid
  JOIN curated.replacement_anchor_embeddings ae ON ae.replacement_wo_uuid = c.replacement_wo_uuid
  WHERE pe.embedding.status = ''
    AND ae.anchor_embedding.status = ''
    AND pe.embedding.result IS NOT NULL
    AND ae.anchor_embedding.result IS NOT NULL
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
**Inputs.** `scored_precursors`, `pma_agent_analytics.wo_workorders` (texts), infra connection (Gemini).

**Instructions.**
1. For each surviving pair (a replacement/precursor candidate that passed L2 threshold + top-K), send the **precursor text** and the **replacement anchor text** to a
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
WITH wo_text AS (
  SELECT
    w.workorder_uuid AS wo_uuid,
    TRIM(
      CONCAT(
        COALESCE(w.remarks, ''),
        CASE WHEN COALESCE(w.remarks, '') <> '' THEN '\n' ELSE '' END,
        COALESCE(
          (
            SELECT ARRAY_TO_STRING(
              ARRAY(
                SELECT TRIM(
                  CONCAT(
                    COALESCE(ws.description, ''),
                    CASE
                      WHEN TRIM(COALESCE(a.action_text, '')) <> '' THEN CONCAT('\n', a.action_text)
                      ELSE ''
                    END
                  )
                )
                FROM UNNEST(IFNULL(w.work_steps, [])) AS ws
                LEFT JOIN UNNEST(IFNULL(ws.actions, [])) AS a
                WHERE TRIM(CONCAT(COALESCE(ws.description, ''), COALESCE(a.action_text, ''))) <> ''
              ),
              '\n\n'
            )
          ),
          ''
        )
      )
    ) AS description_concat
  FROM `pma_agent_analytics.wo_workorders` w
),
anchor_by_replacement AS (
  SELECT
    replacement_wo_uuid,
    ANY_VALUE(anchor_text) AS anchor_text
  FROM curated.dim_reference_set
  GROUP BY replacement_wo_uuid
),
pairs AS (
  SELECT
    sp.*,
    wt.description_concat AS precursor_text,
    ar.anchor_text
  FROM curated.scored_precursors sp
  JOIN wo_text wt
    ON wt.wo_uuid = sp.precursor_wo_uuid
  JOIN anchor_by_replacement ar
    ON ar.replacement_wo_uuid = sp.replacement_wo_uuid
),
prompts AS (
  SELECT
    p.*,
    CONCAT(
      'You are an aircraft maintenance analyst. A component was REPLACED (the anchor). ',
      'Decide whether the EARLIER work order describes an emerging symptom/defect that plausibly PRECEDED and relates to that replacement.\n',
      '- "symptom": earlier WO reports a degrading defect on the same component/system.\n',
      '- "routine": scheduled check/inspection/servicing, not a failure symptom.\n',
      '- "cannibalization": part removed to serve another aircraft.\n',
      '- "duplicate": same event / the replacement itself restated.\n',
      '- "unrelated": different system or coincidental text match.\n',
      'Return ONLY JSON with keys: verdict, confidence, reason.\n\n',
      'REPLACEMENT (anchor): <<<', COALESCE(p.anchor_text, ''), '>>>\n',
      'EARLIER WORK ORDER: <<<', COALESCE(p.precursor_text, ''), '>>>'
    ) AS prompt
  FROM pairs p
),
judged_raw AS (
  SELECT
    pr.*,
    AI.GENERATE(
      pr.prompt,
      endpoint => 'gemini-2.5-pro',
      output_schema => 'verdict STRING, confidence FLOAT64, reason STRING'
    ) AS llm
  FROM prompts pr
),
parsed AS (
  SELECT
    jr.*,
    TO_JSON_STRING(
      STRUCT(
        jr.llm.verdict AS verdict,
        jr.llm.confidence AS confidence,
        jr.llm.reason AS reason
      )
    ) AS result_json_blob,
    JSON_VALUE(jr.llm.full_response, '$.candidates[0].content.parts[0].text') AS full_response_text
  FROM judged_raw jr
),
normalized AS (
  SELECT
    p.*,
    TRIM(
      REGEXP_REPLACE(
        REGEXP_REPLACE(
          COALESCE(
            JSON_VALUE(p.result_json_blob, '$'),
            p.result_json_blob,
            p.full_response_text,
            ''
          ),
          r'^```(?:json)?\\s*',
          ''
        ),
        r'\\s*```$',
        ''
      )
    ) AS model_json_text
  FROM parsed p
),
judged AS (
  SELECT
    n.* EXCEPT(llm, result_json_blob, full_response_text, model_json_text),
    LOWER(COALESCE(n.llm.verdict, JSON_VALUE(n.model_json_text, '$.verdict'))) AS verdict,
    COALESCE(n.llm.confidence, SAFE_CAST(JSON_VALUE(n.model_json_text, '$.confidence') AS FLOAT64)) AS llm_conf,
    COALESCE(n.llm.reason, JSON_VALUE(n.model_json_text, '$.reason')) AS reason,
    n.model_json_text,
    n.full_response_text AS llm_full_response,
    n.llm.status AS llm_status
  FROM normalized n
)
SELECT *
FROM judged
WHERE verdict = 'symptom';
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
  SELECT wo_uuid AS id, embedding.result AS embedding, aircraft_reg, ata_chapter, tac
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
| `SIM_THRESHOLD` | min cosine sim for L2 | Step 06 | 0.62 (starting point; tune via Step 10 check 4) |
| `K_PRECURSORS` | max precursors per replacement | Step 06 | 30 (starting point; tune via Step 10 check 4) |
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
5. **Infra (rest):** Vertex resources + Step 04 embeddings. Embed reference-set WOs.
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

---

<a name="11"></a>
## 11. Final PMA online agent plan (production inference)

This section is the final runbook for using the offline artifacts in live prediction when a new
AMOS `transferWorkorder` arrives.

### 11.1 Objective

Given a newly created work order, return recommend-only guidance:
- likely replacement component risk,
- expected TAC timing window,
- confidence plus evidence.

No automatic maintenance action is triggered.

### 11.2 Inputs required at runtime

1. New WO event payload (`transferWorkorder` XML):
   - `workorderNumber`, `workorder uuid`
   - `aircraftRegistration`
   - text fields from `workStep/description` plus `action/actionText`
   - `ataChapter`
   - `closingTac` if present (otherwise latest known TAC for aircraft)
2. Offline artifacts from Sections 1-10:
   - `curated.dim_focus_components`
   - `curated.dim_reference_set`
   - `curated.fct_lead_time_samples`
   - Vector Search index plus endpoint loaded from `curated.wo_embeddings`
3. Runtime configuration:
   - `SIM_THRESHOLD`
   - `K_NEIGHBORS`
   - `MIN_SAMPLE`
   - `MAX_CV`

### 11.3 Online decision flow

#### Step O1 - Parse and normalize incoming WO

Build normalized payload:
- `wo_uuid`, `wo_id`, `aircraft_reg`, `ata_chapter`, `current_tac`
- `wo_text` = concatenated symptom text from step/action fields

Reject early if:
- missing `aircraft_reg`, or
- empty `wo_text`.

Return `no_reliable_prediction` with reason.

#### Step O2 - Optional component gate (Top-3 focus)

Use LLM/text gate (same schema from Step 07) to map the WO to a likely `component_key` in
`curated.dim_focus_components`.

If no confident match to Top-3 component family, return:
- `decision = out_of_scope`
- `reason = component_not_in_focus_set`

#### Step O3 - Embed new WO and retrieve nearest historical patterns

1. Generate embedding for `wo_text` with the same model used in Step 04.
2. Query Vector Search endpoint for top `K_NEIGHBORS`.
3. Apply filters:
   - same `aircraft_reg` reference eligibility (via `dim_reference_set` join policy)
   - optional ATA pre-filter (ATA-4)
   - similarity `>= SIM_THRESHOLD`.

If no neighbors survive, return `no_reliable_prediction`.

#### Step O4 - Map neighbors to validated precursor-replacement knowledge

For surviving neighbors, join to validated history:
- `curated.adjudicated_precursors` (verdict = symptom provenance)
- `curated.fct_lead_time_samples` (lead cycles distribution)

Aggregate by candidate `component_key`:
- sample size `n`
- `p50`, `p90` lead cycles
- `mean`, `stdev`, `cv`
- weighted similarity score.

#### Step O5 - Compute TAC forecast window

For each candidate:
- `predicted_replacement_tac_p50 = current_tac + lead_p50`
- `predicted_replacement_tac_p90 = current_tac + lead_p90`

Confidence policy:
- high: `n >= MIN_SAMPLE` and `cv <= MAX_CV` and strong similarity
- medium: partial threshold pass
- low: otherwise (or suppress recommendation based on policy)

#### Step O6 - Build explainable recommendation output

Return top candidates sorted by confidence and similarity with:
- predicted component/key,
- TAC p50/p90 window,
- confidence tier,
- evidence list (matched WO ids plus similarities),
- rationale text.

### 11.4 Response contract (recommend-only)

```json
{
  "aircraft": "G-RUKI",
  "workorder_id": "WO-12345",
  "decision": "recommendation",
  "recommendations": [
    {
      "component_key": "15800-029-3|CABIN",
      "predicted_replacement": {
        "lead_tac_p50": 85,
        "lead_tac_p90": 255,
        "tac_p50": 42310,
        "tac_p90": 42480
      },
      "confidence": {
        "level": "medium",
        "similarity": 0.82,
        "sample_size": 23,
        "cv": 0.41
      },
      "evidence": [
        {"wo_id": "WO-9981", "sim": 0.86},
        {"wo_id": "WO-10021", "sim": 0.81}
      ],
      "action": "recommend_inspection_or_part_planning"
    }
  ]
}
```

Fallback contract when insufficient evidence:

```json
{
  "aircraft": "G-RUKI",
  "workorder_id": "WO-12345",
  "decision": "no_reliable_prediction",
  "reason": "insufficient_similarity_or_sample_size"
}
```

### 11.5 Runtime guardrails

1. Never auto-create maintenance orders or part swaps.
2. Suppress output when evidence quality is weak.
3. Always include explainability fields for audit.
4. Log all inference inputs/outputs and model versions.
5. Track outcomes for recalibration:
   - whether replacement later occurred,
   - actual TAC at replacement,
   - forecast error by component.

### 11.6 Post-deployment calibration loop

Run weekly or monthly recalibration:
1. Join predictions to eventual replacement outcomes.
2. Compute precision-at-K and TAC forecast error.
3. Re-tune `SIM_THRESHOLD`, `K_NEIGHBORS`, `MIN_SAMPLE`, `MAX_CV`.
4. Rebuild offline artifacts on cadence (scheduler) and redeploy index snapshot.

### 11.7 Go-live checklist (final)

- [ ] Step 10 offline validation signed off.
- [ ] Vector index smoke tests pass on known historical symptoms.
- [ ] Online response contract wired to AMOS event consumer.
- [ ] Monitoring dashboard created (volume, no-prediction rate, confidence mix, latency).
- [ ] Human reviewer workflow documented for recommendation consumption.
- [ ] Rollback plan prepared (disable endpoint or force no-prediction mode).
