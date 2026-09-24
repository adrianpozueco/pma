# Frontend PMA Recommendation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the frontend's synthetic analysis mock with the live `POST /workorders/analyze` response and render the PMA recommendation card.

**Architecture:** A pure TS adapter (`src/data/api.ts`) builds the request and maps the backend JSON to a new `Outlook` union in `src/domain.ts`. `src/data/client.ts` keeps the `AnalysisClient` interface but calls the adapter; samples are two real XMLs under `public/samples/`. `App.tsx` `Results` renders the three `Outlook` states. Vite proxies `/api` → `127.0.0.1:8000`.

**Tech Stack:** React 18, TypeScript 5.9, Vite 8, node:test (Node 24 native TS stripping), jsdom, Playwright.

**Spec:** `docs/superpowers/specs/2026-09-24-frontend-pma-recommendation-design.md`

## Global Constraints

- No backend changes. No new npm dependencies.
- `src/data/api.ts` must use only erasable TS syntax (no `enum`, no parameter properties) so `node --test tests/api.test.ts` runs it directly.
- API base: `import.meta.env?.VITE_API_BASE ?? "/api"` (guard `import.meta.env` — undefined under plain node).
- Real backend fixtures: `frontend/tests/fixtures/analysis/landing_light_rh.json`, `aft_smoke_detector.json` (captured 2026-09-24 with `PMA_PREDICTION_ENABLED=true`).
- Local backend: `PMA_PREDICTION_ENABLED=true uv run uvicorn pm_agent.fast_api_app:app --port 8000` from repo root.

## Pinned UI copy (Task 2 renders, Task 3 asserts — exact strings)

| Element | Text |
|---|---|
| Status pill | `RECOMMENDATION` / `HISTORICAL INTERVAL` / `ESTIMATE UNAVAILABLE` |
| Rec. header (`h4.rec-title`) | `Plan inspection / part replacement — 72605363-1\|RH` (`Monitor — …` for monitor) |
| Headline (`.rec-headline`) | `Replace around TAC 14,704 (p50) · 14,984 (p90) · 15,015 (p95)` |
| Status (`.rec-status`) | `Latest known TAC 14,516 → 188 cycles before p50` |
| Confidence (`.rec-confidence`) | `MEDIUM CONFIDENCE` + `n=5 · CV 0.67 · similarity 0.88` |
| Basis | `Basis: similar work orders + lead-time history` |
| Notice | `Heuristic estimate, not a calibrated forecast.` |
| Evidence item | `WO 190599369` + `similarity 0.90` |
| Interval title | `Observed historical interval (not a forecast)` |
| Footer | sample: `Example work order · Live PMA analysis`; upload: `Uploaded XML · Live PMA analysis` |
| Unreachable error | `Analysis service unreachable. Start the backend: PMA_PREDICTION_ENABLED=true uv run uvicorn pm_agent.fast_api_app:app --port 8000` |
| Sample cards | `RH landing light power supply` (PN `72605363-1`), `AFT cargo smoke detector` (PN `473597-5`) |

## Review Focus

1. Backend down → fetch rejects → user sees the unreachable message, not a blank/stuck spinner (Task 1 test `unreachable`).
2. `tac_*` all null (no TAC anchor) → headline shows lead cycles only, no `NaN`/`null` text (Task 1 `formatHeadline` test).
3. p95 null → headline omits the p95 segment (Task 1 test).
4. Reference TAC already past p90/p95 → status `past p90` / `already past p95` (Task 1 test).
5. Cancel mid-request → AbortError is not shown as an error (Task 1 test: abort propagates `AbortError`; App already ignores aborted controllers).

---

### Task 1: Contract, adapter, client, proxy, samples (lead — done inline)

**Files:**
- Modify: `frontend/src/domain.ts` (`Outlook`, `WorkOrderDocument.xml`, `Sample.file`, `Sample.icon`)
- Create: `frontend/src/data/api.ts`
- Modify: `frontend/src/data/client.ts` (drop synthetic mocks; samples fetch `/samples/*.xml`)
- Modify: `frontend/src/data/xml.ts` (return `xml` on the document)
- Create: `frontend/vite.config.ts` (proxy)
- Create: `frontend/public/samples/landing_light_rh.xml`, `aft_smoke_detector.xml`
- Test: `frontend/tests/api.test.ts`; `package.json` script `test:unit`

**Interfaces (Produces):**
```ts
// domain.ts
export interface RecommendationOutlook { status: "recommendation"; componentKey: string; action: string; basis: string;
  tac: Quantiles; lead: Quantiles; referenceTac: number | null;
  confidence: { level: string; similarity: number | null; sampleSize: number; cv: number | null };
  evidence: { woId: string; sim: number | null }[]; cyclesPerDay: number; asOf: string; limitations: string[] }
export interface Quantiles { p50: number | null; p90: number | null; p95: number | null }
export type Outlook = RecommendationOutlook
  | { status: "interval"; componentKey: string | null; p50: number; p90: number; n: number; aircraft: number | null; asOf: string; limitations: string[] }
  | { status: "unavailable"; reason: string; missing: string[]; limitations: string[] };
// api.ts
export function buildAnalyzeUrl(base: string, order: {status: string; id: string}, orderCount: number, now: Date): string
export function mapAnalysis(response: unknown, cyclesPerDay: number): Outlook
export function actionText(action: string): string
export function basisText(basis: string): string
export function formatHeadline(o: RecommendationOutlook): string
export function formatStatus(o: RecommendationOutlook): string | null
export function formatConfidence(o: RecommendationOutlook): string   // "n=5 · CV 0.67 · similarity 0.88"
export async function analyzeXml(xml: string, url: string, signal: AbortSignal, fetchImpl?: typeof fetch): Promise<unknown>
export const UNREACHABLE_MESSAGE: string
```
`asOf` is the date part (`YYYY-MM-DD`) of `analysis_as_of`.

- [ ] Step 1: write `tests/api.test.ts` covering: both real fixtures → `recommendation` with exact numbers; synthetic interval and unavailable responses; headline without p95 / without TACs; status past p90 / past p95; URL (`historical_replay` for "Closed…", `selected_wo_id` only when >1 order); `analyzeXml` → 422 message, 503 message, fetch reject → `UNREACHABLE_MESSAGE`, abort → rejects `AbortError`.
- [ ] Step 2: `node --test tests/api.test.ts` → FAIL (module missing).
- [ ] Step 3: implement `api.ts`, domain changes, client, xml, vite config.
- [ ] Step 4: `node --test tests/api.test.ts` PASS; `npx tsc --noEmit` may fail only in `App.tsx` (Task 2 owns it).
- [ ] Step 5: commit `feat(frontend): live analyze adapter and PMA outlook contract`.

### Task 2: Results UI + copy (agent A)

**Files:** Modify `frontend/src/App.tsx` (`Results`, `analyse()` announcement, sample list/storage copy, footer, architecture copy mentioning synthetic samples), `frontend/src/styles.css` (only new `.rec-*` classes, reuse tokens), `frontend/README.md` ("What works" + run-with-backend section).

**Consumes:** Task 1 `Outlook`, `formatHeadline`, `formatStatus`, `formatConfidence`, `actionText`, `basisText` from `./data/api`; existing `futureDate`, `formatWindow` in App.tsx.

- Recommendation: pill `RECOMMENDATION` (yellow); `.result-notice` with the heuristic notice; `h4.rec-title`, `.rec-headline`, `.rec-status` (omit when null); metrics: "Cycles before p50" card showing `lead.p50` (or "Unavailable") with caption `p90 {lead.p90} · p95 {lead.p95}`; "Estimated calendar window" = `formatWindow(futureDate(asOf, lead.p50, cpd), futureDate(asOf, lead.p90, cpd))`, caption `Assuming N flight cycle(s) / day`; `.rec-confidence` pill + detail; basis line; evidence list `WO … · similarity 0.90`; limitations list.
- Interval: pill `HISTORICAL INTERVAL`, title, `p50 X cycles · p90 Y cycles (n=…, … aircraft)`.
- Unavailable: pill `ESTIMATE UNAVAILABLE`, reason, missing list, limitations.
- Remove the static "Replacement recommendation: unavailable" block and the illustrative timeline.
- Verify: `npm run build` passes; commit `feat(frontend): render live PMA recommendation card`.

### Task 3: Test suites migrate to mocked backend (agent B, parallel with Task 2)

**Files:** Modify `frontend/tests/ui.test.mjs`, `frontend/tests/journey.spec.ts`, `frontend/playwright.config.ts` (timeout comment only).

- ui.test.mjs: before `window.eval(bundle)`, install `window.fetch` stub: `/samples/*.xml` → the file from `public/samples/`; `/api/workorders/analyze` → fixture JSON by a per-test choice (default `landing_light_rh.json`), option to reject (unreachable) or 422. Replace synthetic-sample tests (nozzle/boiler/oven, "illustrative") with the two real samples and pinned copy. Drop the 2600 ms mock-pipeline waits.
- journey.spec.ts: `page.route("**/api/workorders/analyze**")` fulfilling fixtures; samples served by Vite from `public/`. Same replacements. Add unreachable test (`route.abort()`).
- Verify: `npm run build && node --test tests/ui.test.mjs`, `npx playwright test` pass once Task 2 lands; commit `test(frontend): mock live analyze endpoint`.

### Task 4: E2E + review (lead)

- Run app against local backend with both samples; screenshot. Whole-branch review.
