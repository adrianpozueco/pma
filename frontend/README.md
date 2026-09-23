# Ryanair Maintenance Intelligence frontend

A standalone React/TypeScript prototype for the Google ASL capstone. It develops
the agreed UI independently of the Python/ADK backend.

## Run

Use Node 22.12+ (or a supported newer LTS release):

```sh
cd frontend
npm ci
npm run dev
```

Vite prints the local URL. No Google credentials or running Python service are
required. For a single file that opens directly in a browser:

```sh
npm run build:preview
open dist/preview.html
```

`dist/preview.html` contains the built JS and CSS, uses no external assets, and
can be shared as a prototype. Regenerate it after source changes. The normal
`npm run build` output is available for future static hosting or Cloud Run.

## What works

- XML selection/drag-and-drop, file limits, browser-side AMOS field preview,
  multiple-work-order selection and explicit component selection.
- A Cloud Storage tab using three **local synthetic scenarios**. This represents
  the planned storage picker; it does not list or download real bucket objects.
- Review, cancellable simulated analysis, illustrative forecast and missing-data
  results, evidence expansion, restart and the educational How it works view.
- Responsive Ryanair blue/yellow styling with a text wordmark. Exact official
  typography and logo artwork still need verification; the current system-font
  treatment is provisional.

No live ADK, BigQuery, manual-search or Cloud Storage calls are made. XML remains
in the browser. Uploaded XML never receives a sample forecast. The nozzle and
oven examples use openly labelled fictional ranges; the boiler scenario shows
missing data. No replacement policy or maintenance deadline is asserted.

The browser parser is a preview, not a replacement for authoritative server-side
AMOS validation or historical cutoff handling. It displays recorded component
roles and aircraft counters without treating them as current component age.

## Backend integration boundary

`src/domain.ts` defines UI-facing data types. `src/data/client.ts` is the only
current data adapter. Screens depend on these operations:

| Operation                              | Purpose                                            |
| -------------------------------------- | -------------------------------------------------- |
| `listSamples(signal)`                  | List display metadata for selectable work orders   |
| `loadSample(id, signal)`               | Load the selected input into a reviewable document |
| `readUpload(file, signal)`             | Produce the same document shape from an upload     |
| `analyze(request, signal, onProgress)` | Emit stage statuses and return an outlook          |

These are frontend interface names, **not proposed fixed HTTP routes**. Once the
backend contract settles, implement a live adapter mapping its responses and
events into these types. Preserve source identity, selection, cancellation and
unavailable reasons. Keep raw XML or its server artifact reference in the adapter
so the authoritative service receives the original input, not just UI fields.

The prototype's outlook union deliberately supports only illustrative and
unavailable results. Add a distinct validated-model result variant when the real
forecast contract is agreed, including model provenance, units, date assumptions
and uncertainty. Do not relabel the mock ranges as live model output.

Storage credentials and allowed bucket/prefix configuration belong on the server.
Cloud object generation and upload artifact identity should be preserved there.
Render genuine progress events when connected; the present timers are simulations.

## Verification

```sh
npm run build
npm run test:ui
npx playwright install chromium
npm run test:e2e
```

`test:ui` (16 tests) exercises the production bundle in JSDOM: upload provenance,
per-sample outcomes, counter semantics, multi-order selection, upload guards,
utilisation bounds and the stale-result cancellation paths. It does not verify
layout, focus or overflow. `test:e2e` (20 tests) covers those in a real browser,
plus keyboard tab navigation, console/network cleanliness and 390px overflow on
every screen. It always starts its own Vite server on port **4174**, so a dev
server on 4173 is never graded by mistake. To use an existing Chrome installation:

```sh
PLAYWRIGHT_CHROMIUM_EXECUTABLE='/Applications/Google Chrome.app/Contents/MacOS/Google Chrome' npm run test:e2e
```

With system Chrome the run can occasionally sit for several minutes after the
last test: Chrome starts `GoogleUpdater`, which holds the worker's stdio pipe
open. The tests themselves take about a minute. Playwright's bundled browser
(`npx playwright install chromium`, then run without the variable) avoids it.

The assertions guarding each product invariant were mutation-tested: fourteen
deliberate regressions (no abort, sample forecast on uploads, missing
recommendation, wrong aircraft counter, swapped scenario ranges, and so on)
each turn at least one suite red.

Run `npm run build:preview` **last**: `npm run build` (also run by `test:ui`)
clears `dist/`, including a previously generated `preview.html`.

See [the working plan](../docs/ryanair-ui-plan.md) for the capstone scope, hosting
proposal and future AMOS ingestion story.
