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
- Responsive Ryanair blue/yellow styling with brand-referenced decorative
  motifs (see "Branding" below).

No live ADK, BigQuery, manual-search or Cloud Storage calls are made. XML remains
in the browser. Uploaded XML never receives a sample forecast. The nozzle and
oven examples use openly labelled fictional ranges; the boiler scenario shows
missing data. No replacement policy or maintenance deadline is asserted.

The browser parser is a preview, not a replacement for authoritative server-side
AMOS validation or historical cutoff handling. It displays recorded component
roles and aircraft counters without treating them as current component age.

## Branding

Colours, typography and decorative motifs are sourced from an internal Ryanair
Labs presentation template (a slide deck reference, not an approved design
system): deck blue `#073590` and deck yellow `#f1c933` carry primary actions,
headings and selected states; deep navy `#1d1d67` and a light accent blue
`#2091eb` are used on dark surfaces only; body copy and secondary text sit on
the deck's neutral greys (`#2e2e2e` / `#555555`). Every token is a CSS custom
property in `src/styles.css` (`--blue`, `--navy`, `--yellow`, `--bright`, plus
the grey scale) rather than a hardcoded colour, so it stays easy to correct
once official brand values are confirmed.

Typography is Roboto for body text and Oswald for uppercase display titles
(Oswald substitutes for the deck's proprietary Knockout face; it is only ever
applied through CSS `text-transform: uppercase`, never by changing the DOM
copy itself). Both are vendored as woff2 files under `src/assets/fonts` with
their original OFL licences included alongside them.

Decorative brand motifs (the swoosh, dot-grid, circuit-field texture and the
harp mark drawn from the Ryanair Labs lockup) are implemented as CSS
`::before`/`::after` pseudo-elements referencing images in `src/assets/brand`,
never as real DOM nodes or TypeScript imports — this keeps them out of the
accessibility tree and out of screen-reader/keyboard navigation. Every motif
is `pointer-events: none` and layered under text and interactive content. The
one exception is the small `.harp` glyph rendered inline in the header, which
is a real `<span>` but carries `aria-hidden="true"` for the same reason.

All Ryanair colours, typography and motifs here are provisional and derived
from that internal presentation template, not from Ryanair's approved brand
guidelines; provenance/disclaimer copy calls this out in the UI itself (for
example "Synthetic samples", "Illustrative forecast", "Not connected") and
that copy is deliberately kept at a legible size (12px or larger) so it is
never mistaken for real branding or live data.

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

## Known limitations

- Colours, typography and motifs are provisional (see "Branding" above); no
  official Ryanair brand asset bundle or approved design system was available.
- The band caption wraps to two lines below 651px — unavoidable while keeping
  provenance text at a legible 12px.
- At 1280×800 "Choose XML file" is above the fold; "Review work order" still
  needs a short scroll.
- Screen-reader announcements are implemented (per-stage live text, a
  persistent `role="status"` outcome) but were only tested in Chrome, not
  with a real screen reader such as NVDA or VoiceOver.
- `issuedAt` is parsed from uploaded XML but not displayed.

See [the working plan](../docs/ryanair-ui-plan.md) for the capstone scope, hosting
proposal and future AMOS ingestion story.
