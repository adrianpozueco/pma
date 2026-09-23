import { expect, test, type Page } from "@playwright/test";
import path from "node:path";

const NOZZLE_UPLOAD = path.resolve(
  "../tests/fixtures/workorders/demo_nozzle_upload.xml",
);
const DISTINCT_ORDERS = path.resolve(
  "tests/fixtures/two_workorders_distinct.xml",
);
const ISSUE_AND_CLOSING = path.resolve(
  "tests/fixtures/issue_and_closing_counters.xml",
);
// The mock pipeline is 4 stages x 650 ms = 2600 ms. Absence tests wait past it.
const PAST_FULL_PIPELINE_MS = 3500;

async function selectSample(page: Page, name: string) {
  await page.getByRole("tab", { name: "Cloud Storage" }).click();
  await page.getByRole("button", { name: new RegExp(name) }).click();
  await page.getByRole("button", { name: "Review work order" }).click();
}

async function uploadNozzleFixture(page: Page) {
  // reset() keeps the last source tab, so choose the upload tab explicitly.
  await page.getByRole("tab", { name: "Upload XML" }).click();
  await page.getByLabel("Upload work-order XML").setInputFiles(NOZZLE_UPLOAD);
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
    if (msg.type() === "error") errors.push(`console: ${msg.text()}`);
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

test("sample completes the presentation journey with explicit illustrative provenance", async ({
  page,
}) => {
  const errors = trackErrors(page);
  await page.setViewportSize({ width: 1440, height: 1120 });
  await page.goto("/");
  await expect(reviewButton(page)).toBeDisabled();
  await page.screenshot({
    path: "/tmp/ryanair-ui-desktop.png",
    fullPage: true,
  });
  await selectSample(page, "Fuel-injection nozzle");
  await expect(
    page.getByText("DEMO-NOZZLE-001", { exact: false }).first(),
  ).toBeVisible();
  await expect(currentStep(page)).toContainText("Review component");
  await cyclesInput(page).fill("0");
  await expect(analyseButton(page)).toBeDisabled();
  await cyclesInput(page).fill("6");
  await analyseButton(page).click();
  // I2: the sample forecast is labelled fictional and illustrative.
  await expect(
    page.getByText("ILLUSTRATIVE FORECAST", { exact: true }),
  ).toBeVisible();
  await expect(page.getByText(/^A fictional result/)).toBeVisible();
  await expect(currentStep(page)).toContainText("Replacement outlook");
  // I7: the nozzle keeps its own range.
  await expect(page.getByText("180–260", { exact: true })).toBeVisible();
  await expect(workspace(page)).not.toContainText("300–420");
  // I3
  await expect(
    page.getByText("Replacement recommendation: unavailable"),
  ).toBeVisible();
  // Results identity: status in the eyebrow, role on the PN/serial line.
  await expect(page.locator(".result-identity .eyebrow")).toHaveText(
    "DEMO-737 / DEMO-NOZZLE-001 · Open work order",
  );
  await expect(page.locator(".result-identity p").last()).toHaveText(
    /PN 2085M31G03\s*·\s*Serial DEMO-NOZZLE-01\s*·\s*Recorded component/,
  );
  // Calendar window at 6 flight cycles / day: +30 and +44 days from 23 Sep 2026.
  await expect(page.locator(".date-metric")).toHaveText(
    /23 Oct\s*[–-]\s*6 Nov 2026/,
  );
  await expect(page.getByText("Assuming 6 flight cycles / day")).toBeVisible();
  await expect(page.locator(".cycle-band")).toBeVisible();
  await expect(page.locator(".cycle-band-caption")).toHaveText(
    /180–260 flight cycles\s+remaining · window assumes 6 flight cycles\s*\/ day/,
  );
  await expect(page.getByText(/This is a closed work order/)).toHaveCount(0);
  await page
    .getByText("Comparable maintenance events", { exact: true })
    .click();
  await expect(page.getByText(/No live records were queried/)).toBeVisible();
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

test("missing-data scenario never presents fabricated cycles or a zero estimate", async ({
  page,
}) => {
  await page.goto("/");
  await selectSample(page, "Water boiler");
  await analyseButton(page).click();
  await expect(
    page.getByText("ESTIMATE UNAVAILABLE", { exact: true }),
  ).toBeVisible();
  await expect(page.getByText("What’s needed for an estimate")).toBeVisible();
  // I7: the boiler has no range at all.
  await expect(workspace(page)).not.toContainText("180–260");
  await expect(workspace(page)).not.toContainText("300–420");
  await expect(
    page.getByText("ILLUSTRATIVE FORECAST", { exact: true }),
  ).toHaveCount(0);
  await expect(page.locator(".date-metric")).toHaveText("Unavailable");
  await expect(
    page.getByText("No date is inferred from incomplete data"),
  ).toBeVisible();
  await expect(
    page.getByText("Verified component installation date and counters"),
  ).toBeVisible();
  // I3
  await expect(
    page.getByText("Replacement recommendation: unavailable"),
  ).toBeVisible();
});

test("oven sample keeps its own 300–420 illustrative outcome", async ({
  page,
}) => {
  await page.goto("/");
  await selectSample(page, "Convection oven");
  await expect(page.locator(".component-option")).toContainText(
    "PN 8201-11-0000-01",
  );
  await analyseButton(page).click();
  await expect(
    page.getByText("ILLUSTRATIVE FORECAST", { exact: true }),
  ).toBeVisible();
  await expect(page.getByText("300–420", { exact: true })).toBeVisible();
  await expect(workspace(page)).not.toContainText("180–260");
  await expect(page.locator(".result-identity p").last()).toContainText(
    "PN 8201-11-0000-01",
  );
  await expect(page.getByText(/^A fictional result/)).toBeVisible();
  await expect(
    page.getByText("Replacement recommendation: unavailable"),
  ).toBeVisible();
  // 300/6 = 50 days, 420/6 = 70 days from 23 Sep 2026.
  await expect(page.locator(".date-metric")).toHaveText(
    /12 Nov\s*[–-]\s*2 Dec 2026/,
  );
  await expect(page.locator(".cycle-band-caption")).toContainText(
    "300–420 flight cycles",
  );
});

test("aircraft cycles at issue are shown as an aircraft counter, never component age", async ({
  page,
}) => {
  await page.goto("/");
  await selectSample(page, "Fuel-injection nozzle");
  await expect(cyclesAtIssue(page)).toHaveText("12,480");
  await expect(
    page.getByText(/Aircraft total cycles are not component age/),
  ).toBeVisible();
  await page.getByRole("button", { name: "Start again" }).click();
  await uploadNozzleFixture(page);
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

test("uploaded XML preserves its own facts and cannot receive a sample forecast", async ({
  page,
}) => {
  await page.goto("/");
  await uploadNozzleFixture(page);
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
  // I1: no sample forecast of any kind.
  await expect(
    page.getByText("ESTIMATE UNAVAILABLE", { exact: true }),
  ).toBeVisible();
  await expect(page.getByText(/Your XML has been read locally/)).toBeVisible();
  await expect(
    page.getByText("ILLUSTRATIVE FORECAST", { exact: true }),
  ).toHaveCount(0);
  await expect(workspace(page)).not.toContainText("180–260");
  await expect(workspace(page)).not.toContainText("300–420");
  await expect(workspace(page)).not.toContainText("A fictional result");
  await expect(page.locator(".date-metric")).toHaveText("Unavailable");
  // I3
  await expect(
    page.getByText("Replacement recommendation: unavailable"),
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

test("Cloud Storage lists the three synthetic samples with provenance", async ({
  page,
}) => {
  await page.goto("/");
  await expect(page.locator(".preview-bar")).toContainText(
    "Synthetic samples · Cloud services not connected",
  );
  await page.getByRole("tab", { name: "Cloud Storage" }).click();
  const cards = page.locator(".sample-card");
  await expect(cards).toHaveCount(3);
  await expect(cards.nth(0)).toContainText("PN 2085M31G03");
  await expect(cards.nth(1)).toContainText("PN 62197-301-001");
  await expect(cards.nth(2)).toContainText("PN 8201-11-0000-01");
  await expect(
    page.getByText("Synthetic samples. No bucket is connected yet."),
  ).toBeVisible();
  for (let i = 0; i < 3; i++)
    await expect(cards.nth(i)).toHaveAttribute("aria-pressed", "false");
  await cards.filter({ hasText: "Water boiler" }).click();
  await expect(cards.nth(1)).toHaveAttribute("aria-pressed", "true");
  await expect(cards.nth(0)).toHaveAttribute("aria-pressed", "false");
  await expect(cards.nth(2)).toHaveAttribute("aria-pressed", "false");
  await expect(page.locator(".selected-file")).toContainText(
    "Synthetic sample",
  );
});

test("switching source tab clears the selection and any error", async ({
  page,
}) => {
  await page.goto("/");
  await page.getByRole("tab", { name: "Cloud Storage" }).click();
  await page.getByRole("button", { name: /Fuel-injection nozzle/ }).click();
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
  await selectSample(page, "Fuel-injection nozzle");
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
  // 180/2 = 90 days, 260/2 = 130 days from 23 Sep 2026.
  await expect(page.locator(".date-metric")).toHaveText(
    /22 Dec 2026\s*[–-]\s*31 Jan 2027/,
  );
  await expect(page.getByText("Assuming 2 flight cycles / day")).toBeVisible();
  await page
    .getByRole("button", { name: "Analyse another work order" })
    .click();
  await selectSample(page, "Fuel-injection nozzle");
  // F7: the reset restores the default assumption.
  await expect(cyclesInput(page)).toHaveValue("6");
  await cyclesInput(page).fill("1");
  await analyseButton(page).click();
  await expect(page.getByText("Assuming 1 flight cycle / day")).toBeVisible();
  await expect(page.locator(".cycle-band-caption")).toContainText(
    "window assumes 1 flight cycle / day",
  );
});

test("Start again on review resets the utilisation to 6", async ({ page }) => {
  await page.goto("/");
  await selectSample(page, "Water boiler");
  await cyclesInput(page).fill("3.5");
  await page.getByRole("button", { name: "Start again" }).click();
  await expect(workspace(page)).toBeFocused();
  await selectSample(page, "Water boiler");
  await expect(cyclesInput(page)).toHaveValue("6");
});

test("cancelled analysis never lands a stale result", async ({ page }) => {
  await page.goto("/");
  await selectSample(page, "Fuel-injection nozzle");
  await analyseButton(page).click();
  await expect(
    page.getByRole("button", { name: "Cancel analysis" }),
  ).toBeVisible();
  await page.getByRole("button", { name: "Cancel analysis" }).click();
  await expect(analyseButton(page)).toBeVisible();
  await expect(currentStep(page)).toContainText("Review component");
  // Deliberate wait past the full 2600 ms mock pipeline: an absence test must
  // give a leaked run every chance to land before asserting it did not.
  await page.waitForTimeout(PAST_FULL_PIPELINE_MS);
  await expect(currentStep(page)).toContainText("Review component");
  await expect(
    page.getByText("ILLUSTRATIVE FORECAST", { exact: true }),
  ).toHaveCount(0);
  await expect(workspace(page)).not.toContainText("180–260");
  await expect(analyseButton(page)).toBeEnabled();

  // A fresh run on another sample still produces its own outcome.
  await page.getByRole("button", { name: "Start again" }).click();
  await selectSample(page, "Water boiler");
  await analyseButton(page).click();
  await expect(
    page.getByText("ESTIMATE UNAVAILABLE", { exact: true }),
  ).toBeVisible();
  await expect(page.getByText("Water boiler", { exact: true })).toBeVisible();
  await expect(workspace(page)).not.toContainText("180–260");
});

test("Start again during analysis abandons the run", async ({ page }) => {
  await page.goto("/");
  await selectSample(page, "Fuel-injection nozzle");
  await analyseButton(page).click();
  await expect(
    page.getByRole("button", { name: "Cancel analysis" }),
  ).toBeVisible();
  await page.getByRole("button", { name: "Start again" }).click();
  await expect(currentStep(page)).toContainText("Select work order");
  await expect(workspace(page)).toBeFocused();
  // Deliberate wait past the full 2600 ms mock pipeline. reset() nulls the
  // document, so a leaked run renders no forecast text — but it would still
  // advance the stepper to "Replacement outlook". Only the stepper catches it.
  await page.waitForTimeout(PAST_FULL_PIPELINE_MS);
  await expect(currentStep(page)).toContainText("Select work order");
  await expect(currentStep(page)).not.toContainText("Replacement outlook");
  await expect(
    page.getByRole("tablist", { name: "Work order source" }),
  ).toBeVisible();
  await expect(reviewButton(page)).toBeDisabled();
  await expect(
    page.getByText("ILLUSTRATIVE FORECAST", { exact: true }),
  ).toHaveCount(0);
  await expect(workspace(page)).not.toContainText("180–260");
});

test("focus returns to the workspace after every journey move", async ({
  page,
}) => {
  await page.goto("/");
  await selectSample(page, "Fuel-injection nozzle");
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
  await page.goto("/");
  await selectSample(page, "Convection oven");
  await analyseButton(page).click();
  await expect(page.getByText("300–420", { exact: true })).toBeVisible();
  await page
    .getByRole("button", { name: "Analyse another work order" })
    .click();
  await page.getByRole("button", { name: "How it works" }).click();
  await page.getByRole("button", { name: "Explore the demo" }).click();
  await uploadNozzleFixture(page);
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
  await expect(page.locator(".sample-card")).toHaveCount(3);
  await expectNoHorizontalOverflow(page);
  await page.getByRole("button", { name: /Fuel-injection nozzle/ }).click();
  await reviewButton(page).click();
  await expect(analyseButton(page)).toBeVisible();
  await expectNoHorizontalOverflow(page);
  await analyseButton(page).click();
  await expect(
    page.getByText("ILLUSTRATIVE FORECAST", { exact: true }),
  ).toBeVisible();
  await expectNoHorizontalOverflow(page);
  await page.getByRole("button", { name: "How it works" }).click();
  await expect(page.getByText("A connected cloud workflow.")).toBeVisible();
  await expectNoHorizontalOverflow(page);
  await page.getByRole("button", { name: "Explore the demo" }).click();
  await expect(
    page.getByText("ILLUSTRATIVE FORECAST", { exact: true }),
  ).toBeVisible();
  // Uploaded closed order: longer strings on review and results.
  await page
    .getByRole("button", { name: "Ryanair Maintenance Intelligence home" })
    .click();
  await uploadNozzleFixture(page);
  await expect(page.locator(".component-option")).toHaveCount(3);
  await expectNoHorizontalOverflow(page);
  await page.getByRole("radio").nth(2).check();
  await analyseButton(page).click();
  await expect(
    page.getByText("ESTIMATE UNAVAILABLE", { exact: true }),
  ).toBeVisible();
  await expectNoHorizontalOverflow(page);
});
