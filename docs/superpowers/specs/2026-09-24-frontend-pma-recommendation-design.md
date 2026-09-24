# Frontend ↔ PMA recommendation — design

Date: 2026-09-24 · Branch: `feat/frontend-pma-recommendation` (from `dev`)

## Goal

Show the live PMA analysis (condensed recommendation, historical interval, or
"unavailable") in the React frontend (`frontend/`) instead of the synthetic
mock results. Success: selecting a sample or uploading an AMOS XML runs the
real deterministic analysis and renders the recommendation card with the same
numbers the ADK chat reply shows.

## Decisions (agreed in chat)

1. Channel: REST `POST /workorders/analyze` (structured JSON, no LLM). The chat
   markdown / fenced JSON block is not parsed.
2. Samples: two real XMLs replace the synthetic nozzle/boiler/oven mocks:
   - `example_workorders/05_rh_landing_light_power_supply_72605363-1.xml`
   - `aft_smoke_detector_with_pn.xml`
   Copied to `frontend/public/samples/`, fetched, then take the upload path.
3. Display: cycles first (TAC p50/p90/p95 + lead cycles), calendar window
   second, derived from the existing cycles/day input.
4. Backend: local FastAPI only (`uvicorn pm_agent.fast_api_app:app`, :8000).
   Vite dev server proxies `/api/*` → `http://127.0.0.1:8000`. Base path is
   `import.meta.env.VITE_API_BASE ?? "/api"`. Agent Runtime deployment does
   not expose the route and is out of scope.

No backend code changes.

## Request

`POST {base}/workorders/analyze?mode=…&analysis_as_of=…[&selected_wo_id=…]`,
body = raw XML bytes (`Content-Type: application/xml`).

- `mode`: `historical_replay` when the selected WO state is closed (`C`),
  else `new_work_order`.
- `analysis_as_of`: `new Date().toISOString()`.
- `selected_wo_id`: only when the document contains more than one WO; the
  preview's WO number is sent.
- `target_part_number`: never sent (out-of-scope parts return 422; the
  backend resolves the component itself).

The browser XML preview (`data/xml.ts`) stays: it drives WO/component
selection and the work-order facts panel. The upload keeps the original XML
text (sample or file) so the same bytes are posted.

## Response → UI mapping

New `Outlook` union in `domain.ts` (replaces `illustrative`):

```ts
type Outlook =
  | { status: "recommendation"; componentKey: string; action: "recommend_inspection_or_part_planning" | "monitor" | string;
      basis: string; tac: {p50,p90,p95: number|null}; lead: {p50,p90,p95: number|null};
      referenceTac: number|null; confidence: {level, similarity, sampleSize, cv};
      evidence: {woId: string; sim: number|null}[]; cyclesPerDay: number; asOf: string; limitations: string[] }
  | { status: "interval"; componentKey: string|null; p50: number; p90: number; n: number; aircraft: number|null;
      asOf: string; limitations: string[] }
  | { status: "unavailable"; reason: string; missing: string[]; limitations: string[] };
```

Rules, first match wins (the recommendation is layered on top of the base
decision, so it is checked first):

1. `pma.recommendation` non-null → `recommendation`.
2. `pma.decision === "historical_interval"` and `pma.interval` → `interval`.
3. else → `unavailable`; `reason` from a `pma.reason` → text map (fallback to
   the raw code), `missing` = `pma.missing`, `limitations` = top-level
   `limitations`.

Numbers are rounded to integers for TACs/cycles, 2 decimals for sim/CV.

## UI

Recommendation card:
- Header: action text (`Plan inspection / part replacement` | `Monitor`) — component key.
- Headline: `Replace around TAC 14,704 (p50) · 14,984 (p90) · 15,015 (p95)`
  (p95 omitted when null; TAC line omitted when all `tac_*` null → show lead
  cycles only).
- Status: `latest known TAC 14,516 → 188 cycles before p50` / `past p50` /
  `past p90` / `already past p95` (same rules as `chat.py`).
- Secondary: `≈ <date> – <date> at N cycles/day` from lead p50/p90 + asOf.
- Confidence pill `MEDIUM` + `n=5 · CV 0.67 · similarity 0.88`, basis label,
  notice "Heuristic estimate, not a calibrated forecast".
- Evidence list: `WO 190599369 · similarity 0.90`.

Interval card: "Observed historical interval (not a forecast)", p50/p90, n,
aircraft. Unavailable card: existing styling, reason + missing + limitations.

Progress: one backend call; all four stages `running` until it resolves, then
`complete` (recommendation/interval) or `workorder` complete + others
`unavailable`.

## Errors

- `fetch` rejects or HTTP 502/504 → "Analysis service unreachable. Start the
  backend: `uv run uvicorn pm_agent.fast_api_app:app --port 8000`."
- 503 → "History service unavailable."
- 422 → `detail.message`.
- Abort → existing cancel behaviour.

## Testing

- Adapter unit tests (`node --test`): `mapAnalysis(response)` for
  recommendation, interval, unavailable, plus request-param builder (mode,
  selected_wo_id). Fixtures in `frontend/tests/fixtures/`, one saved from a
  real local call where available.
- Playwright `journey.spec.ts`: `page.route("**/api/workorders/analyze")`
  returns the recommendation fixture; assert headline + evidence.
- `npm run build` (tsc) passes.
- Manual E2E against local uvicorn with both samples.

## Out of scope

Chat panel, Agent Runtime / Cloud Run deployment, current-TAC input,
backend changes.
