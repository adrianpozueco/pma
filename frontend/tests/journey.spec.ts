import { expect, test, type Page, type Route } from "@playwright/test";
import { readFileSync } from "node:fs";
import path from "node:path";

// A closed upload with header, partOff and partOn components.
const CLOSED_UPLOAD = path.resolve(
  "../tests/fixtures/workorders/demo_nozzle_upload.xml",
);
const LANDING_XML = path.resolve("public/samples/landing_light_rh.xml");
const DISTINCT_ORDERS = path.resolve(
  "tests/fixtures/two_workorders_distinct.xml",
);
const ISSUE_AND_CLOSING = path.resolve(
  "tests/fixtures/issue_and_closing_counters.xml",
);

// Captured backend responses; the analyze endpoint is always mocked, so the
// suite never needs the FastAPI backend.
const analysisFixture = (name: string) =>
  JSON.parse(
    readFileSync(path.resolve(`tests/fixtures/analysis/${name}.json`), "utf8"),
  );
const LANDING = analysisFixture("landing_light_rh");
const SMOKE = analysisFixture("aft_smoke_detector");
const withPma = (pma: Record<string, unknown>) => ({
  ...LANDING,
  pma: { ...LANDING.pma, recommendation: null, interval: null, ...pma },
});
const INTERVAL = withPma({
  decision: "historical_interval",
  interval: { p50: 300, p90: 900, n: 13, aircraft: 10 },
});
const UNAVAILABLE = withPma({
  decision: "no_reliable_prediction",
  reason: "component_not_in_focus_set",
  missing: ["Verified component installation date"],
});
const UNREACHABLE =
  "Analysis service unreachable. Start the backend: PMA_PREDICTION_ENABLED=true uv run uvicorn pm_agent.fast_api_app:app --port 8000";
const ANALYZE = "**/api/workorders/analyze**";

type Recorded = { method: string; body: string | null; url: URL };

/** Answers every analyze call with `body`; returns the recorded requests. */
async function mockAnalyze(page: Page, body: unknown = LANDING) {
  const requests: Recorded[] = [];
  await page.unroute(ANALYZE);
  await page.route(ANALYZE, (route) => {
    const request = route.request();
    requests.push({
      method: request.method(),
      body: request.postData(),
      url: new URL(request.url()),
    });
    return route.fulfill({ json: body });
  });
  return requests;
}

/** Holds analyze calls until release(); a late fulfil after an abort is fine. */
async function holdAnalyze(page: Page, body: unknown = LANDING) {
  const held: Route[] = [];
  await page.unroute(ANALYZE);
  await page.route(ANALYZE, (route) => {
    held.push(route);
  });
  return {
    sent: () => held.length,
    release: () =>
      Promise.all(held.map((route) => route.fulfill({ json: body }).catch(() => {}))),
  };
}

async function selectSample(page: Page, name: string) {
  await page.getByRole("tab", { name: "Cloud Storage" }).click();
  await page.getByRole("button", { name: new RegExp(name) }).click();
  await page.getByRole("button", { name: "Review work order" }).click();
}

async function uploadFile(page: Page, file: string) {
  // reset() keeps the last source tab, so choose the upload tab explicitly.
  await page.getByRole("tab", { name: "Upload XML" }).click();
  await page.getByLabel("Upload work-order XML").setInputFiles(file);
  await page.getByRole("button", { name: "Review work order" }).click();
}

function uploadXml(page: Page, name: string, xml: string | Buffer) {
  return page.getByLabel("Upload work-order XML").setInputFiles({
    name,
    mimeType: "application/xml",
    buffer: typeof xml === "string" ? Buffer.from(xml) : xml,
  });
}

const currentStep = (page: Page) =>
  page.locator('.journey li[aria-current="step"]');
const workspace = (page: Page) => page.locator("main.workspace");
const cyclesAtIssue = (page: Page) =>
  page
    .locator(".facts > div")
    .filter({ hasText: "Aircraft cycles at issue" })
    .locator("strong");
const analyseButton = (page: Page) =>
  page.getByRole("button", { name: "Analyse component" });
const reviewButton = (page: Page) =>
  page.getByRole("button", { name: "Review work order" });
const cyclesInput = (page: Page) =>
  page.getByLabel("Expected flight cycles per day");
const headline = (page: Page) => page.locator(".rec-headline");

async function expectNoHorizontalOverflow(page: Page) {
  expect(
    await page.evaluate(
      () =>
        document.documentElement.scrollWidth <=
        document.documentElement.clientWidth,
    ),
  ).toBe(true);
}

/** Collects every console error, uncaught page error and failed HTTP response. */
function trackErrors(page: Page) {
  const errors: string[] = [];
  page.on("console", (msg) => {
    if (msg.type() === "error")
      errors.push(`console: ${msg.text()} @ ${msg.location().url}`);
  });
  page.on("pageerror", (error) => errors.push(`pageerror: ${error.message}`));
  page.on("response", (response) => {
    if (response.status() >= 400)
      errors.push(`http ${response.status()}: ${response.url()}`);
  });
  page.on("requestfailed", (request) =>
    errors.push(`requestfailed: ${request.url()}`),
  );
  return errors;
}

test.beforeEach(async ({ page }) => {
  await mockAnalyze(page);
});

test("landing-light sample posts its XML and renders the live recommendation", async ({
  page,
}) => {
  const errors = trackErrors(page);
  const requests = await mockAnalyze(page, LANDING);
  await page.setViewportSize({ width: 1440, height: 1120 });
  await page.goto("/");
  await expect(reviewButton(page)).toBeDisabled();
  await page.screenshot({
    path: "/tmp/ryanair-ui-desktop.png",
    fullPage: true,
  });
  await selectSample(page, "RH landing light power supply");
  await expect(page.getByText("Work order EX-LLT-001")).toBeVisible();
  await expect(currentStep(page)).toContainText("Review component");
  // I4: this order records no issue TAC.
  await expect(cyclesAtIssue(page)).toHaveText("Not recorded");
  await cyclesInput(page).fill("0");
  await expect(analyseButton(page)).toBeDisabled();
  await cyclesInput(page).fill("6");
  await analyseButton(page).click();
  await expect(currentStep(page)).toContainText("Replacement outlook");

  expect(requests).toHaveLength(1);
  expect(requests[0].method).toBe("POST");
  expect(requests[0].body).toBe(readFileSync(LANDING_XML, "utf8"));
  expect(requests[0].url.searchParams.get("mode")).toBe("new_work_order");
  expect(requests[0].url.searchParams.get("analysis_as_of")).toBeTruthy();
  expect(requests[0].url.searchParams.has("selected_wo_id")).toBe(false);

  await expect(
    page.getByText("RECOMMENDATION", { exact: true }),
  ).toBeVisible();
  await expect(page.locator("h4.rec-title")).toHaveText(
    "Plan inspection / part replacement — 72605363-1|RH",
  );
  await expect(headline(page)).toHaveText(
    "Replace around TAC 14,704 (p50) · 14,984 (p90) · 15,015 (p95)",
  );
  await expect(page.locator(".rec-status")).toHaveText(
    "Latest known TAC 14,516 → 188 cycles before p50",
  );
  await expect(page.locator(".rec-confidence")).toContainText(
    "MEDIUM CONFIDENCE",
  );
  await expect(page.locator(".rec-confidence")).toContainText(
    "n=5 · CV 0.67 · similarity 0.88",
  );
  await expect(
    page.getByText("Basis: similar work orders + lead-time history"),
  ).toBeVisible();
  await expect(
    page.getByText("Heuristic estimate, not a calibrated forecast."),
  ).toBeVisible();
  await expect(page.getByText("WO 190599369")).toBeVisible();
  await expect(page.getByText("similarity 0.90").first()).toBeVisible();
  await expect(
    page.getByText("Example work order · Live PMA analysis"),
  ).toBeVisible();
  await expect(page.locator(".result-identity h3")).toHaveText("72605363-1|RH");
  // Each citation [N] in the basis line points at the matching WO entry.
  const cites = page.locator(".rec-basis a.rec-cite");
  await expect(cites).toHaveText(["[1]", "[2]", "[3]"]);
  for (const [i, woId] of ["190599369", "55702121", "190362777"].entries()) {
    await expect(cites.nth(i)).toHaveAttribute("href", `#rec-evidence-${i + 1}`);
    await expect(
      page.locator(`ul.rec-evidence li#rec-evidence-${i + 1} strong`),
    ).toHaveText(`[${i + 1}] WO ${woId}`);
  }
  // Calendar window at 6 flight cycles / day from analysis_as_of 2026-09-24.
  await expect(workspace(page)).toContainText(/26 Oct\s*[–-]\s*11 Dec 2026/);
  await expect(page.getByText("Assuming 6 flight cycles / day")).toBeVisible();
  await expect(workspace(page)).not.toContainText("NaN");
  await expect(workspace(page)).not.toContainText("undefined");
  await expect(page.getByText(/This is a closed work order/)).toHaveCount(0);
  await page.screenshot({
    path: "/tmp/ryanair-ui-results.png",
    fullPage: true,
  });
  await page
    .getByRole("button", { name: "Analyse another work order" })
    .click();
  await expect(reviewButton(page)).toBeDisabled();
  // F2
  await expect(workspace(page)).toBeFocused();
  expect(errors).toEqual([]);
});

test("smoke-detector sample renders its own recommendation", async ({
  page,
}) => {
  await mockAnalyze(page, SMOKE);
  await page.goto("/");
  await selectSample(page, "AFT cargo smoke detector");
  await expect(page.locator(".component-option")).toContainText(
    "PN 473597-5",
  );
  await analyseButton(page).click();
  await expect(page.locator("h4.rec-title")).toHaveText(
    "Plan inspection / part replacement — 473597-5|AFT",
  );
  await expect(headline(page)).toHaveText(
    "Replace around TAC 19,073 (p50) · 19,859 (p90) · 20,187 (p95)",
  );
  await expect(page.locator(".rec-status")).toHaveText(
    "Latest known TAC 18,660 → 413 cycles before p50",
  );
  await expect(page.locator(".rec-confidence")).toContainText(
    "n=16 · CV 1.10 · similarity 0.74",
  );
  await expect(workspace(page)).not.toContainText("14,704");
});

test("a historical interval is labelled as observed, not forecast", async ({
  page,
}) => {
  await mockAnalyze(page, INTERVAL);
  await page.goto("/");
  await selectSample(page, "RH landing light power supply");
  await analyseButton(page).click();
  await expect(
    page.getByText("HISTORICAL INTERVAL", { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByText("Observed historical interval (not a forecast)"),
  ).toBeVisible();
  await expect(workspace(page)).toContainText(
    "p50 300 cycles · p90 900 cycles (n=13, 10 aircraft)",
  );
  await expect(page.locator(".rec-confidence")).toHaveCount(0);
  await expect(workspace(page)).not.toContainText("Replace around TAC");
  await expect(page.getByText("RECOMMENDATION", { exact: true })).toHaveCount(
    0,
  );
});

test("an unavailable estimate shows the reason and never fabricates cycles", async ({
  page,
}) => {
  await mockAnalyze(page, UNAVAILABLE);
  await page.goto("/");
  await selectSample(page, "RH landing light power supply");
  await analyseButton(page).click();
  await expect(
    page.getByText("ESTIMATE UNAVAILABLE", { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByText(
      "This component is not one of the tracked focus components.",
    ),
  ).toBeVisible();
  await expect(
    page.getByText("Verified component installation date"),
  ).toBeVisible();
  await expect(headline(page)).toHaveCount(0);
  await expect(workspace(page)).not.toContainText("14,704");
  await expect(workspace(page)).not.toContainText("NaN");
});

test("an unreachable backend returns to review with the start-up hint", async ({
  page,
}) => {
  const errors = trackErrors(page);
  await page.unroute(ANALYZE);
  await page.route(ANALYZE, (route) => route.abort());
  await page.goto("/");
  await selectSample(page, "RH landing light power supply");
  await analyseButton(page).click();
  await expect(page.getByRole("alert")).toContainText(UNREACHABLE);
  await expect(currentStep(page)).toContainText("Review component");
  await expect(page.locator(".result-panel")).toHaveCount(0);
  await expect(analyseButton(page)).toBeEnabled();
  // Once the backend is up, the same review can be analysed again.
  await mockAnalyze(page, LANDING);
  await analyseButton(page).click();
  await expect(headline(page)).toContainText("14,704");
  await expect(page.getByRole("alert")).toHaveCount(0);
  // The aborted analyze call is intentional; nothing else may fail.
  expect(
    errors.filter((error) => !error.includes("/api/workorders/analyze")),
  ).toEqual([]);
});

test("aircraft cycles at issue are shown as an aircraft counter, never component age", async ({
  page,
}) => {
  await mockAnalyze(page, UNAVAILABLE);
  await page.goto("/");
  await selectSample(page, "RH landing light power supply");
  await expect(cyclesAtIssue(page)).toHaveText("Not recorded");
  await expect(
    page.getByText(/Aircraft total cycles are not component age/),
  ).toBeVisible();
  await page.getByRole("button", { name: "Start again" }).click();
  await uploadFile(page, CLOSED_UPLOAD);
  // The fixture records a closing TAC of 3210 but no issue TAC.
  await expect(cyclesAtIssue(page)).toHaveText("Not recorded");
  await expect(
    page.getByText(/Aircraft total cycles are not component age/),
  ).toBeVisible();
  await expect(workspace(page)).not.toContainText("3,210");
  await expect(workspace(page)).not.toContainText("3210");
  await page.getByRole("radio").nth(2).check();
  await analyseButton(page).click();
  await expect(
    page.getByText("ESTIMATE UNAVAILABLE", { exact: true }),
  ).toBeVisible();
  await expect(workspace(page)).not.toContainText("3,210");
  await expect(workspace(page)).not.toContainText("3210");
  // With BOTH counters recorded, only the issue TAC may surface; the closing
  // TAC must never be shown as the component's counter.
  await page.getByRole("button", { name: "Start again" }).click();
  await page.getByRole("tab", { name: "Upload XML" }).click();
  await page
    .getByLabel("Upload work-order XML")
    .setInputFiles(ISSUE_AND_CLOSING);
  await page.getByRole("button", { name: "Review work order" }).click();
  await expect(cyclesAtIssue(page)).toHaveText("4,321");
  await expect(workspace(page)).not.toContainText("9,876");
  await expect(workspace(page)).not.toContainText("9876");
});

test("uploaded closed XML keeps its own facts and is replayed historically", async ({
  page,
}) => {
  const requests = await mockAnalyze(page, UNAVAILABLE);
  await page.goto("/");
  await uploadFile(page, CLOSED_UPLOAD);
  await expect(page.getByText("Work order DEMO-XML-ONLY-2085")).toBeVisible();
  await expect(page.getByText(/COPPER-FINCH-41/)).toBeVisible();
  // I5 on the review screen.
  await expect(page.getByText(/This is a closed work order/)).toBeVisible();
  await expect(page.locator(".review-header .subtle-text")).toHaveText(
    "Closed · historical review",
  );
  await expect(analyseButton(page)).toBeDisabled();
  // Role labels: one recorded, one removed, one installed.
  const options = page.locator(".component-option");
  await expect(options).toHaveCount(3);
  await expect(options.nth(0)).toContainText("Serial DEMO-OFF-41");
  await expect(options.nth(0)).toContainText("Recorded component");
  await expect(options.nth(1)).toContainText("Serial DEMO-OFF-41");
  await expect(options.nth(1)).toContainText(
    "DEMO-POSITION · Removed component",
  );
  await expect(options.nth(2)).toContainText("Serial DEMO-ON-42");
  await expect(options.nth(2)).toContainText(
    "DEMO-POSITION · Installed component",
  );
  await options.filter({ hasText: "DEMO-ON-42" }).getByRole("radio").check();
  await expect(analyseButton(page)).toBeEnabled();
  await analyseButton(page).click();
  await expect(
    page.getByText("ESTIMATE UNAVAILABLE", { exact: true }),
  ).toBeVisible();
  expect(requests[0].body).toBe(readFileSync(CLOSED_UPLOAD, "utf8"));
  expect(requests[0].url.searchParams.get("mode")).toBe("historical_replay");
  await expect(
    page.getByText("Uploaded XML · Live PMA analysis"),
  ).toBeVisible();
  // I5 on the results screen too, and the status rides in the eyebrow.
  await expect(page.getByText(/This is a closed work order/)).toBeVisible();
  await expect(page.locator(".result-identity .eyebrow")).toContainText(
    "DEMO-XML-ONLY-2085 · Closed · historical review",
  );
  await expect(page.locator(".result-identity p").last()).toHaveText(
    /PN 2085M31G03\s*·\s*Serial DEMO-ON-42\s*·\s*Installed component/,
  );
});

test("a multi-order upload sends the selected work order id", async ({
  page,
}) => {
  const requests = await mockAnalyze(page, LANDING);
  await page.goto("/");
  await page.getByLabel("Upload work-order XML").setInputFiles(DISTINCT_ORDERS);
  await reviewButton(page).click();
  await page.getByLabel("Select work order").selectOption("1");
  await page.getByRole("radio").nth(1).check();
  await analyseButton(page).click();
  await expect(headline(page)).toBeVisible();
  expect(requests[0].body).toBe(readFileSync(DISTINCT_ORDERS, "utf8"));
  expect(requests[0].url.searchParams.get("selected_wo_id")).toBe(
    "WO-BRAVO-2",
  );
});

test("multiple work orders with missing components cannot start analysis", async ({
  page,
}) => {
  await page.goto("/");
  await page
    .getByLabel("Upload work-order XML")
    .setInputFiles(
      path.resolve("../tests/fixtures/workorders/two_workorders.xml"),
    );
  await reviewButton(page).click();
  const selector = page.getByLabel("Select work order");
  await expect(selector).toBeVisible();
  await selector.selectOption("1");
  await expect(selector).toHaveValue("1");
  await expect(page.locator(".component-option")).toHaveCount(0);
  await expect(
    page.getByText(/No component part numbers were found/),
  ).toBeVisible();
  await expect(analyseButton(page)).toBeDisabled();
});

test("switching between distinct work orders swaps every fact and the component list", async ({
  page,
}) => {
  await page.goto("/");
  await page.getByLabel("Upload work-order XML").setInputFiles(DISTINCT_ORDERS);
  await reviewButton(page).click();
  const selector = page.getByLabel("Select work order");
  // Every order renders as an always-present <option>, so scope to the panel.
  const facts = page.locator(".review-panel .facts");
  const symptom = page.locator(".symptom p");
  const components = page.locator(".component-option");

  const expectOrderA = async () => {
    await expect(selector).toHaveValue("0");
    await expect(facts).toContainText("EI-AAA");
    await expect(facts).not.toContainText("EI-BBB");
    await expect(facts).toContainText("ALPHA-TYPE");
    await expect(cyclesAtIssue(page)).toHaveText("1,111");
    await expect(symptom).toHaveText(/Order A symptom: galley oven/);
    await expect(components).toHaveCount(1);
    await expect(components).toContainText("ALPHA-SN-1");
    // A single component is selected automatically.
    await expect(components.getByRole("radio")).toBeChecked();
    await expect(analyseButton(page)).toBeEnabled();
    await expect(page.locator(".context-card h3")).toHaveText("EI-AAA");
  };

  await expectOrderA();
  await selector.selectOption("1");
  await expect(selector).toHaveValue("1");
  await expect(facts).toContainText("EI-BBB");
  await expect(facts).not.toContainText("EI-AAA");
  await expect(facts).toContainText("BRAVO-TYPE");
  await expect(cyclesAtIssue(page)).toHaveText("2,222");
  await expect(symptom).toHaveText(/Order B symptom: water boiler/);
  await expect(symptom).not.toContainText("Order A");
  await expect(components).toHaveCount(2);
  await expect(components.nth(0)).toContainText("BRAVO-SN-1");
  await expect(components.nth(1)).toContainText("BRAVO-SN-2");
  // Auto-selection only fires for a single component.
  await expect(page.getByRole("radio", { checked: true })).toHaveCount(0);
  await expect(analyseButton(page)).toBeDisabled();
  await expect(page.locator(".context-card h3")).toHaveText("EI-BBB");
  await selector.selectOption("0");
  await expectOrderA();
});

test("malformed and entity-bearing XML is rejected with an actionable error", async ({
  page,
}) => {
  await page.goto("/");
  await uploadXml(page, "broken.xml", "<workorder>");
  await expect(page.getByRole("alert")).toContainText(
    "This XML could not be read",
  );
  await expect(reviewButton(page)).toBeDisabled();
  await uploadXml(
    page,
    "entity.xml",
    '<!DOCTYPE test [<!ENTITY value "demo">]><workorder/>',
  );
  await expect(page.getByRole("alert")).toContainText(
    "entity declarations are not supported",
  );
  await expect(reviewButton(page)).toBeDisabled();
});

test("a CDATA mention of <!DOCTYPE is accepted while real doctypes stay rejected", async ({
  page,
}) => {
  await page.goto("/");
  const rejection =
    "XML document types and entity declarations are not supported";
  await uploadXml(
    page,
    "doctype.xml",
    "<!DOCTYPE workorder><workorder><workorderNumber>X</workorderNumber></workorder>",
  );
  await expect(page.getByRole("alert")).toContainText(rejection);
  await expect(reviewButton(page)).toBeDisabled();
  await uploadXml(
    page,
    "entity.xml",
    '<!DOCTYPE workorder [<!ENTITY marker "demo">]><workorder><workorderNumber>&marker;</workorderNumber></workorder>',
  );
  await expect(page.getByRole("alert")).toContainText(rejection);
  await expect(reviewButton(page)).toBeDisabled();
  await uploadXml(
    page,
    "cdata.xml",
    `<?xml version="1.0" encoding="UTF-8"?>
<amosTransportEnvelope><payload type="transferWorkorder"><transferWorkorder>
  <workorder uuid="wo-cdata"><workorderNumber>CDATA-1</workorderNumber>
    <workorderHeader><workorderState>O</workorderState>
      <component><partNumber>2085M31G03</partNumber><serialNumber>CD-1</serialNumber></component>
    </workorderHeader>
    <workSteps><workStep uuid="step-cdata"><description><![CDATA[log line mentioning <!DOCTYPE html>]]></description></workStep></workSteps>
  </workorder>
</transferWorkorder></payload></amosTransportEnvelope>`,
  );
  await expect(page.locator(".selected-file")).toContainText("cdata.xml");
  await expect(page.getByRole("alert")).toHaveCount(0);
  await expect(reviewButton(page)).toBeEnabled();
  await reviewButton(page).click();
  await expect(page.getByText("Work order CDATA-1")).toBeVisible();
  await expect(page.locator(".symptom p")).toHaveText(
    "log line mentioning <!DOCTYPE html>",
  );
});

test("upload guards reject empty, oversized and multi-file input; errors can be dismissed", async ({
  page,
}) => {
  await page.goto("/");
  await uploadXml(page, "empty.xml", Buffer.alloc(0));
  await expect(page.getByRole("alert")).toContainText("This file is empty");
  await expect(reviewButton(page)).toBeDisabled();

  await uploadXml(page, "huge.xml", Buffer.alloc(26 * 1024 * 1024, 0x20));
  await expect(page.getByRole("alert")).toContainText(
    "exceeds the 25 MiB limit",
  );
  await expect(reviewButton(page)).toBeDisabled();

  const twoFiles = await page.evaluateHandle(() => {
    const transfer = new DataTransfer();
    transfer.items.add(
      new File(["<workorder/>"], "a.xml", { type: "application/xml" }),
    );
    transfer.items.add(
      new File(["<workorder/>"], "b.xml", { type: "application/xml" }),
    );
    return transfer;
  });
  await page.locator(".dropzone").dispatchEvent("drop", {
    dataTransfer: twoFiles,
  });
  await expect(page.getByRole("alert")).toContainText(
    "Choose one XML file at a time",
  );
  await expect(page.locator(".selected-file")).toHaveCount(0);
  await expect(reviewButton(page)).toBeDisabled();

  await page.getByRole("button", { name: "Dismiss error" }).click();
  await expect(page.getByRole("alert")).toHaveCount(0);
  // F2: focus lands on the workspace, not <body>.
  await expect(workspace(page)).toBeFocused();
});

test("Cloud Storage lists both real samples", async ({ page }) => {
  await page.goto("/");
  await page.getByRole("tab", { name: "Cloud Storage" }).click();
  const cards = page.locator(".sample-card");
  await expect(cards).toHaveCount(2);
  await expect(cards.nth(0)).toContainText("RH landing light power supply");
  await expect(cards.nth(0)).toContainText("PN 72605363-1");
  await expect(cards.nth(1)).toContainText("AFT cargo smoke detector");
  await expect(cards.nth(1)).toContainText("PN 473597-5");
  await expect(page.locator(".sample-list")).not.toContainText(/synthetic/i);
  for (let i = 0; i < 2; i++)
    await expect(cards.nth(i)).toHaveAttribute("aria-pressed", "false");
  await cards.filter({ hasText: "AFT cargo smoke detector" }).click();
  await expect(cards.nth(1)).toHaveAttribute("aria-pressed", "true");
  await expect(cards.nth(0)).toHaveAttribute("aria-pressed", "false");
  await expect(page.locator(".selected-file")).toContainText(
    "aft_smoke_detector.xml",
  );
});

test("switching source tab clears the selection and any error", async ({
  page,
}) => {
  await page.goto("/");
  await page.getByRole("tab", { name: "Cloud Storage" }).click();
  await page
    .getByRole("button", { name: /RH landing light power supply/ })
    .click();
  await expect(page.locator(".selected-file")).toBeVisible();
  await expect(reviewButton(page)).toBeEnabled();
  await page.getByRole("tab", { name: "Upload XML" }).click();
  await expect(page.locator(".selected-file")).toHaveCount(0);
  await expect(reviewButton(page)).toBeDisabled();
  // Coming back does not resurrect the old selection.
  await page.getByRole("tab", { name: "Cloud Storage" }).click();
  await expect(page.locator('.sample-card[aria-pressed="true"]')).toHaveCount(
    0,
  );
  await page.getByRole("tab", { name: "Upload XML" }).click();
  await uploadXml(page, "broken.xml", "<workorder>");
  await expect(page.getByRole("alert")).toBeVisible();
  await page.getByRole("tab", { name: "Cloud Storage" }).click();
  await expect(page.getByRole("alert")).toHaveCount(0);
  await expect(page.locator(".error-banner")).toHaveCount(0);
});

test("source tabs follow the ARIA tabs keyboard contract", async ({ page }) => {
  await page.goto("/");
  const upload = page.getByRole("tab", { name: "Upload XML" });
  const storage = page.getByRole("tab", { name: "Cloud Storage" });
  await upload.focus();
  await expect(upload).toHaveAttribute("aria-selected", "true");

  await page.keyboard.press("ArrowRight");
  await expect(storage).toHaveAttribute("aria-selected", "true");
  await expect(upload).toHaveAttribute("aria-selected", "false");
  await expect(storage).toBeFocused();

  await page.keyboard.press("ArrowLeft");
  await expect(upload).toHaveAttribute("aria-selected", "true");
  await expect(storage).toHaveAttribute("aria-selected", "false");
  await expect(upload).toBeFocused();

  await page.keyboard.press("End");
  await expect(storage).toHaveAttribute("aria-selected", "true");
  await expect(storage).toBeFocused();

  await page.keyboard.press("Home");
  await expect(upload).toHaveAttribute("aria-selected", "true");
  await expect(upload).toBeFocused();

  // Wraps from the first tab back to the last.
  await page.keyboard.press("ArrowLeft");
  await expect(storage).toHaveAttribute("aria-selected", "true");
  await expect(storage).toBeFocused();
  await expect(storage).toHaveAttribute("tabindex", "0");
  await expect(upload).toHaveAttribute("tabindex", "-1");
});

test("utilisation bounds guard analysis and drive the calendar window", async ({
  page,
}) => {
  await page.goto("/");
  await selectSample(page, "RH landing light power supply");
  // F1: tiny values stay disabled (1e-9 used to blank the whole app).
  for (const value of ["1e-9", "0.05", "0", "25"]) {
    await cyclesInput(page).fill(value);
    await expect(analyseButton(page)).toBeDisabled();
  }
  await expect(workspace(page)).toBeVisible();
  await expect(page.locator("#workspace-heading")).toHaveText(
    "The right component. The right context.",
  );
  await cyclesInput(page).fill("24");
  await expect(analyseButton(page)).toBeEnabled();
  await cyclesInput(page).fill("2");
  await analyseButton(page).click();
  // 188/2 = 94 days, 468/2 = 234 days from analysis_as_of 2026-09-24.
  await expect(workspace(page)).toContainText(
    /27 Dec 2026\s*[–-]\s*16 May 2027/,
  );
  await expect(page.getByText("Assuming 2 flight cycles / day")).toBeVisible();
  await page
    .getByRole("button", { name: "Analyse another work order" })
    .click();
  await selectSample(page, "RH landing light power supply");
  // F7: the reset restores the default assumption.
  await expect(cyclesInput(page)).toHaveValue("6");
  await cyclesInput(page).fill("1");
  await analyseButton(page).click();
  await expect(page.getByText("Assuming 1 flight cycle / day")).toBeVisible();
  await expect(workspace(page)).toContainText(
    /31 Mar 2027\s*[–-]\s*5 Jan 2028/,
  );
});

test("Start again on review resets the utilisation to 6", async ({ page }) => {
  await page.goto("/");
  await selectSample(page, "AFT cargo smoke detector");
  await cyclesInput(page).fill("3.5");
  await page.getByRole("button", { name: "Start again" }).click();
  await expect(workspace(page)).toBeFocused();
  await selectSample(page, "AFT cargo smoke detector");
  await expect(cyclesInput(page)).toHaveValue("6");
});

test("cancelled analysis never lands a late response", async ({ page }) => {
  await page.goto("/");
  await selectSample(page, "RH landing light power supply");
  const held = await holdAnalyze(page, LANDING);
  await analyseButton(page).click();
  await expect(
    page.getByRole("button", { name: "Cancel analysis" }),
  ).toBeVisible();
  await expect.poll(held.sent).toBe(1);
  await page.getByRole("button", { name: "Cancel analysis" }).click();
  await expect(analyseButton(page)).toBeVisible();
  await expect(currentStep(page)).toContainText("Review component");
  // Deliberate: answer the cancelled request and give it time to land before
  // asserting it did not.
  await held.release();
  await page.waitForTimeout(500);
  await expect(currentStep(page)).toContainText("Review component");
  await expect(page.locator(".result-panel")).toHaveCount(0);
  await expect(page.getByRole("alert")).toHaveCount(0);
  await expect(workspace(page)).not.toContainText("14,704");
  await expect(analyseButton(page)).toBeEnabled();

  // A fresh run on another sample still produces its own outcome.
  await mockAnalyze(page, SMOKE);
  await page.getByRole("button", { name: "Start again" }).click();
  await selectSample(page, "AFT cargo smoke detector");
  await analyseButton(page).click();
  await expect(headline(page)).toContainText("19,073");
  await expect(workspace(page)).not.toContainText("14,704");
});

test("Start again during analysis abandons the run", async ({ page }) => {
  await page.goto("/");
  await selectSample(page, "RH landing light power supply");
  const held = await holdAnalyze(page, LANDING);
  await analyseButton(page).click();
  await expect(
    page.getByRole("button", { name: "Cancel analysis" }),
  ).toBeVisible();
  await expect.poll(held.sent).toBe(1);
  await page.getByRole("button", { name: "Start again" }).click();
  await expect(currentStep(page)).toContainText("Select work order");
  await expect(workspace(page)).toBeFocused();
  // Deliberate: answer the abandoned request. reset() nulls the document, so a
  // leaked run renders no result text — but it would still advance the
  // stepper to "Replacement outlook". Only the stepper catches it.
  await held.release();
  await page.waitForTimeout(500);
  await expect(currentStep(page)).toContainText("Select work order");
  await expect(currentStep(page)).not.toContainText("Replacement outlook");
  await expect(
    page.getByRole("tablist", { name: "Work order source" }),
  ).toBeVisible();
  await expect(reviewButton(page)).toBeDisabled();
  await expect(page.locator(".result-panel")).toHaveCount(0);
  await expect(workspace(page)).not.toContainText("14,704");
});

test("focus returns to the workspace after every journey move", async ({
  page,
}) => {
  await page.goto("/");
  await selectSample(page, "RH landing light power supply");
  await expect(workspace(page)).toBeFocused();
  await page.getByRole("button", { name: "Change source" }).click();
  await expect(currentStep(page)).toContainText("Select work order");
  await expect(workspace(page)).toBeFocused();
  await page.getByRole("button", { name: "How it works" }).click();
  await expect(page.getByText("A connected cloud workflow.")).toBeVisible();
  await page.getByRole("button", { name: "Explore the demo" }).click();
  await expect(currentStep(page)).toContainText("Select work order");
  await expect(page.getByRole("tab", { name: "Cloud Storage" })).toBeVisible();
  await expect(workspace(page)).toBeFocused();
});

test("a full journey produces no console errors, page errors or failed requests", async ({
  page,
}) => {
  const errors = trackErrors(page);
  await mockAnalyze(page, SMOKE);
  await page.goto("/");
  await selectSample(page, "AFT cargo smoke detector");
  await analyseButton(page).click();
  await expect(headline(page)).toContainText("19,073");
  await page
    .getByRole("button", { name: "Analyse another work order" })
    .click();
  await page.getByRole("button", { name: "How it works" }).click();
  await page.getByRole("button", { name: "Explore the demo" }).click();
  await mockAnalyze(page, UNAVAILABLE);
  await uploadFile(page, CLOSED_UPLOAD);
  await page.getByRole("radio").nth(2).check();
  await analyseButton(page).click();
  await expect(
    page.getByText("ESTIMATE UNAVAILABLE", { exact: true }),
  ).toBeVisible();
  await page
    .getByRole("button", { name: "Analyse another work order" })
    .click();
  await expect(reviewButton(page)).toBeDisabled();
  expect(errors).toEqual([]);
});

test("mobile input, review, results and architecture views fit the viewport", async ({
  page,
}) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/");
  await expect(
    page.getByRole("button", { name: "Choose XML file" }),
  ).toBeVisible();
  await expectNoHorizontalOverflow(page);
  await page.screenshot({ path: "/tmp/ryanair-ui-mobile.png", fullPage: true });
  await page.getByRole("tab", { name: "Cloud Storage" }).click();
  await expect(page.locator(".sample-card")).toHaveCount(2);
  await expectNoHorizontalOverflow(page);
  await page
    .getByRole("button", { name: /RH landing light power supply/ })
    .click();
  await reviewButton(page).click();
  await expect(analyseButton(page)).toBeVisible();
  await expectNoHorizontalOverflow(page);
  await analyseButton(page).click();
  await expect(headline(page)).toBeVisible();
  await expectNoHorizontalOverflow(page);
  await page.getByRole("button", { name: "How it works" }).click();
  await expect(page.getByText("A connected cloud workflow.")).toBeVisible();
  await expectNoHorizontalOverflow(page);
  await page.getByRole("button", { name: "Explore the demo" }).click();
  await expect(headline(page)).toBeVisible();
  // Uploaded closed order: longer strings on review and results.
  await mockAnalyze(page, UNAVAILABLE);
  await page
    .getByRole("button", { name: "Ryanair Maintenance Intelligence home" })
    .click();
  await uploadFile(page, CLOSED_UPLOAD);
  await expect(page.locator(".component-option")).toHaveCount(3);
  await expectNoHorizontalOverflow(page);
  await page.getByRole("radio").nth(2).check();
  await analyseButton(page).click();
  await expect(
    page.getByText("ESTIMATE UNAVAILABLE", { exact: true }),
  ).toBeVisible();
  await expectNoHorizontalOverflow(page);
});
