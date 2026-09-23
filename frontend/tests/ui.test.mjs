import assert from "node:assert/strict";
import { readFileSync, readdirSync } from "node:fs";
import { fileURLToPath } from "node:url";
import test from "node:test";
import { JSDOM } from "jsdom";

// Exercises the built React app without a server. This checks interactions and
// content; Playwright remains responsible for real-browser rendering, layout,
// focus and overflow.
const assets = fileURLToPath(new URL("../dist/assets/", import.meta.url));
const bundle = readFileSync(
  `${assets}/${readdirSync(assets).find((name) => name.endsWith(".js"))}`,
  "utf8",
);
const fixture = (path) =>
  readFileSync(new URL(path, import.meta.url), "utf8");
const repoFixture = (name) =>
  fixture(`../../tests/fixtures/workorders/${name}`);
const localFixture = (name) => fixture(`./fixtures/${name}`);

// The mock pipeline is 4 stages x 650ms = 2600ms. Absence tests wait past it.
const PAST_PIPELINE_MS = 3500;
const pause = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
async function waitFor(predicate, message = "Expected UI state") {
  const deadline = Date.now() + 6500;
  while (Date.now() < deadline) {
    if (predicate()) return;
    await pause(20);
  }
  assert.fail(message);
}

let uploadCounter = 0;
async function app(t) {
  const dom = new JSDOM('<div id="root"></div>', {
    url: "http://localhost/",
    runScripts: "dangerously",
    pretendToBeVisual: true,
  });
  const { window } = dom;
  t.after(() => window.close());
  window.eval(bundle);
  const doc = window.document;
  const q = (selector) => doc.querySelector(selector);
  const qa = (selector) => Array.from(doc.querySelectorAll(selector));
  const text = (selector) => q(selector)?.textContent ?? "";
  const body = () => doc.body.textContent;
  const button = (name) =>
    qa("button").find((item) => item.textContent.includes(name));
  /** The stepper item marked aria-current="step". */
  const currentStep = () => text('.journey li[aria-current="step"]');
  /** Scoped label -> value lookup inside the review facts grid. */
  const fact = (label) =>
    qa(".facts > div")
      .find((item) => item.querySelector("span")?.textContent === label)
      ?.querySelector("strong")?.textContent;
  await waitFor(() => button("Choose XML file"), "App rendered");
  // Re-resolve the button on every poll: React may replace the node between
  // renders, and existence and enabled-ness are separate conditions.
  const click = async (name) => {
    await waitFor(() => Boolean(button(name)), `Button exists: ${name}`);
    await waitFor(
      () => button(name).disabled === false,
      `Button enabled: ${name}`,
    );
    button(name).click();
  };
  const sample = async (name) => {
    await click("Cloud Storage");
    await click(name);
    await click("Review work order");
    await waitFor(
      () => currentStep().includes("Review component"),
      "Review step reached",
    );
  };
  /** Resolves when this upload has either produced a selectable document or a
   * fresh error banner, so a previous state can never satisfy the wait. */
  const upload = async (content, filename = `upload-${++uploadCounter}.xml`) => {
    const input = q("input[type=file]");
    const file = new File([content], filename, { type: "application/xml" });
    const previousBanner = q(".error-banner");
    Object.defineProperty(input, "files", {
      configurable: true,
      value: [file],
    });
    input.dispatchEvent(new window.Event("change", { bubbles: true }));
    await waitFor(() => {
      const banner = q(".error-banner");
      if (banner && banner !== previousBanner) return true;
      return (
        text(".selected-file strong") === filename &&
        button("Review work order")?.disabled === false
      );
    }, `Upload settled: ${filename}`);
  };
  const drop = async (files) => {
    const zone = q(".dropzone");
    const event = new window.Event("drop", { bubbles: true, cancelable: true });
    Object.defineProperty(event, "dataTransfer", { value: { files } });
    zone.dispatchEvent(event);
  };
  const setCycles = (value) => {
    const input = q("#cycles");
    // React tracks the last value it saw; the native setter makes the change visible.
    Object.getOwnPropertyDescriptor(
      window.HTMLInputElement.prototype,
      "value",
    ).set.call(input, value);
    input.dispatchEvent(new window.Event("input", { bubbles: true }));
  };
  const chooseOrder = (index) => {
    const select = q("select");
    Object.getOwnPropertyDescriptor(
      window.HTMLSelectElement.prototype,
      "value",
    ).set.call(select, String(index));
    select.dispatchEvent(new window.Event("change", { bubbles: true }));
  };
  const chooseComponent = async (index) => {
    qa("input[type=radio]")[index].click();
    await waitFor(
      () => qa("input[type=radio]")[index].checked,
      `Component ${index} selected`,
    );
  };
  const analyseEnabled = () => button("Analyse component")?.disabled === false;
  const waitForResult = () =>
    waitFor(
      () => Boolean(q(".result-panel")),
      "Results screen rendered",
    );
  return {
    window,
    q,
    qa,
    text,
    body,
    button,
    click,
    sample,
    upload,
    drop,
    setCycles,
    chooseOrder,
    chooseComponent,
    analyseEnabled,
    currentStep,
    fact,
    waitForResult,
  };
}

test("sample journey distinguishes illustrative forecast from operational recommendation", async (t) => {
  const ui = await app(t);
  assert.match(ui.body(), /Synthetic samples · Cloud services not connected/);
  assert.equal(ui.button("Review work order").disabled, true);
  await ui.sample("Fuel-injection nozzle");
  assert.match(ui.body(), /DEMO-NOZZLE-001/);
  // I4: aircraft cycles are labelled as aircraft counters, never component age.
  assert.equal(ui.fact("Aircraft cycles at issue"), "12,480");
  assert.match(ui.body(), /Aircraft total cycles are not component age/);
  assert.equal(ui.analyseEnabled(), true, "single component auto-selected");
  await ui.click("Analyse component");
  await ui.waitForResult();
  assert.match(ui.body(), /ILLUSTRATIVE FORECAST/);
  // I2 / I7
  assert.match(ui.body(), /A fictional result/);
  assert.match(ui.body(), /180–260/);
  assert.doesNotMatch(ui.body(), /300–420/);
  assert.match(ui.body(), /not calculated by ADK or a prediction model/);
  // I3
  assert.match(ui.body(), /Replacement recommendation: unavailable/);
  assert.match(ui.text(".result-identity .eyebrow"), /Open work order/);
  assert.match(ui.text(".result-identity"), /Recorded component/);
  // Calendar window at the default 6 cycles / day.
  assert.match(ui.text(".date-metric"), /23 Oct\s*[–-]\s*6 Nov 2026/);
  assert.match(ui.body(), /Assuming 6 flight cycles \/ day/);
  assert.equal(
    ui.text(".cycle-band-caption"),
    "180–260 flight cycles remaining · window assumes 6 flight cycles / day",
  );
  assert.ok(ui.currentStep().includes("Replacement outlook"));
  await ui.click("Analyse another work order");
  await waitFor(
    () => ui.currentStep().includes("Select work order"),
    "Returned to the start",
  );
  assert.equal(ui.button("Review work order").disabled, true);
});

test("oven sample keeps its own illustrative outcome", async (t) => {
  const ui = await app(t);
  await ui.sample("Convection oven");
  await ui.click("Analyse component");
  await ui.waitForResult();
  assert.match(ui.body(), /ILLUSTRATIVE FORECAST/);
  assert.match(ui.body(), /A fictional result/);
  assert.match(ui.body(), /300–420/);
  assert.doesNotMatch(ui.body(), /180–260/);
  assert.match(ui.text(".result-identity"), /PN 8201-11-0000-01/);
  assert.match(ui.body(), /Replacement recommendation: unavailable/);
  assert.match(ui.text(".date-metric"), /12 Nov\s*[–-]\s*2 Dec 2026/);
});

test("incomplete history returns missing data instead of an invented estimate", async (t) => {
  const ui = await app(t);
  await ui.sample("Water boiler");
  await ui.click("Analyse component");
  await ui.waitForResult();
  assert.match(ui.body(), /ESTIMATE UNAVAILABLE/);
  assert.match(ui.body(), /Verified component installation date and counters/);
  assert.match(ui.body(), /Replacement recommendation: unavailable/);
  assert.equal(ui.text(".date-metric"), "Unavailable");
  assert.doesNotMatch(ui.body(), /180–260|300–420|ILLUSTRATIVE FORECAST/);
});

test("Cloud Storage lists every synthetic sample and marks the chosen one", async (t) => {
  const ui = await app(t);
  await ui.click("Cloud Storage");
  await waitFor(() => ui.qa(".sample-card").length === 3, "Three samples");
  for (const pn of ["2085M31G03", "62197-301-001", "8201-11-0000-01"])
    assert.match(ui.text(".sample-list"), new RegExp(`PN ${pn}`));
  assert.match(ui.body(), /A preview of the planned Cloud Storage samples/);
  assert.match(ui.body(), /Synthetic samples\. No bucket is connected yet\./);
  await ui.click("Water boiler");
  await waitFor(
    () =>
      ui.q('.sample-card[aria-pressed="true"]')?.textContent.includes(
        "Water boiler",
      ),
    "Chosen card pressed",
  );
  assert.equal(ui.qa('.sample-card[aria-pressed="true"]').length, 1);
});

test("uploaded XML retains its own identity and receives no demonstration forecast", async (t) => {
  const ui = await app(t);
  await ui.upload(repoFixture("demo_nozzle_upload.xml"));
  await ui.click("Review work order");
  assert.match(ui.body(), /DEMO-XML-ONLY-2085/);
  assert.match(ui.body(), /COPPER-FINCH-41/);
  assert.match(ui.body(), /closed work order/);
  // I4: the closing aircraft counter (3210) must not pose as component age.
  assert.equal(ui.fact("Aircraft cycles at issue"), "Not recorded");
  assert.doesNotMatch(ui.body(), /3,210|3210/);
  assert.match(ui.body(), /Aircraft total cycles are not component age/);
  // Component roles: header <component>, partOff and partOn.
  const options = ui.qa(".component-option");
  assert.equal(options.length, 3);
  assert.match(options[0].textContent, /Serial DEMO-OFF-41/);
  assert.match(options[0].textContent, /Recorded component/);
  assert.match(
    options[1].textContent,
    /Serial DEMO-OFF-41.*DEMO-POSITION · Removed component/,
  );
  assert.match(
    options[2].textContent,
    /Serial DEMO-ON-42.*DEMO-POSITION · Installed component/,
  );
  assert.equal(ui.button("Analyse component").disabled, true);
  await ui.chooseComponent(2); // the installed DEMO-ON-42, as in Playwright
  await ui.click("Analyse component");
  await ui.waitForResult();
  // I1
  assert.match(ui.body(), /ESTIMATE UNAVAILABLE/);
  assert.match(ui.body(), /Your XML has been read locally/);
  assert.doesNotMatch(ui.body(), /ILLUSTRATIVE FORECAST|180–260|300–420/);
  // I3
  assert.match(ui.body(), /Replacement recommendation: unavailable/);
  // I5 on the results screen.
  assert.match(ui.text(".result-identity .eyebrow"), /Closed · historical review/);
  assert.match(ui.text(".result-panel .notice"), /closed work order/);
  assert.match(
    ui.text(".result-identity"),
    /PN 2085M31G03 · Serial DEMO-ON-42 · Installed component/,
  );
  assert.doesNotMatch(ui.body(), /3,210|3210/);
});

test("multi-order XML switches every detail with the selected order", async (t) => {
  const ui = await app(t);
  await ui.upload(localFixture("jsdom-two_workorders_distinct.xml"));
  await ui.click("Review work order");
  assert.equal(ui.q("select").options.length, 2);
  const facts = () => ui.text(".review-panel .facts");
  const symptom = () => ui.text(".symptom p");
  const orderA = () => {
    assert.match(facts(), /EI-AAA/);
    assert.doesNotMatch(facts(), /EI-BBB/);
    assert.equal(ui.fact("Aircraft cycles at issue"), "1,500");
    assert.match(symptom(), /AMBER-HERON/);
    assert.equal(ui.qa(".component-option").length, 1);
    assert.equal(ui.analyseEnabled(), true, "single component auto-selected");
  };
  orderA();
  ui.chooseOrder(1);
  await waitFor(() => /EI-BBB/.test(facts()), "Order B shown");
  assert.doesNotMatch(facts(), /EI-AAA/);
  assert.match(symptom(), /SLATE-OTTER/);
  assert.doesNotMatch(symptom(), /AMBER-HERON/);
  assert.equal(ui.qa(".component-option").length, 2);
  assert.equal(ui.analyseEnabled(), false, "two components need a choice");
  ui.chooseOrder(0);
  await waitFor(() => /EI-AAA/.test(facts()), "Order A shown again");
  orderA();
});

test("multi-order XML preserves missing component state", async (t) => {
  const ui = await app(t);
  await ui.upload(repoFixture("two_workorders.xml"));
  await ui.click("Review work order");
  const select = ui.q("select");
  assert.equal(select.options.length, 2);
  ui.chooseOrder(1);
  await waitFor(() => ui.q("select").value === "1", "Second order selected");
  assert.match(ui.body(), /No component part numbers were found/);
  assert.equal(ui.button("Analyse component").disabled, true);
});

test("invalid files cannot advance to review", async (t) => {
  const ui = await app(t);
  await ui.upload("hello", "notes.txt");
  assert.match(ui.text(".error-banner"), /Choose an XML file/);
  await ui.upload('<!DOCTYPE test [<!ENTITY value "demo">]><workorder/>');
  assert.match(ui.text(".error-banner"), /entity declarations are not supported/);
  assert.equal(ui.button("Review work order").disabled, true);
  await ui.upload("<!DOCTYPE workorder><workorder/>");
  assert.match(ui.text(".error-banner"), /document types and entity declarations/);
  await ui.upload("<unrelated/>");
  assert.match(ui.text(".error-banner"), /No work orders were found/);
  assert.equal(ui.button("Review work order").disabled, true);
});

test("a CDATA section that mentions a doctype is accepted", async (t) => {
  const ui = await app(t);
  await ui.upload(localFixture("jsdom-cdata_doctype_mention.xml"));
  assert.equal(ui.q(".error-banner"), null);
  await ui.click("Review work order");
  assert.equal(ui.text(".symptom p"), "log line mentioning <!DOCTYPE html>");
  assert.match(ui.body(), /JSDOM-CDATA-1/);
});

test("upload guards reject empty, oversized and multi-file input", async (t) => {
  const ui = await app(t);
  await ui.upload("", "empty.xml");
  assert.match(ui.text(".error-banner"), /This file is empty/);
  await ui.upload(Buffer.alloc(26 * 1024 * 1024, 0x20), "huge.xml");
  assert.match(ui.text(".error-banner"), /exceeds the 25 MiB limit/);
  ui.q('[aria-label="Dismiss error"]').click();
  await waitFor(() => !ui.q(".error-banner"), "Error dismissed");
  await ui.drop([
    new File(["<a/>"], "one.xml", { type: "application/xml" }),
    new File(["<b/>"], "two.xml", { type: "application/xml" }),
  ]);
  await waitFor(
    () => /Choose one XML file at a time/.test(ui.text(".error-banner")),
    "Multi-file drop rejected",
  );
  assert.equal(ui.button("Review work order").disabled, true);
  ui.q('[aria-label="Dismiss error"]').click();
  await waitFor(() => !ui.q(".error-banner"), "Error dismissed again");
});

test("switching source tabs clears the selection and any error", async (t) => {
  const ui = await app(t);
  await ui.click("Cloud Storage");
  await ui.click("Fuel-injection nozzle");
  await waitFor(() => Boolean(ui.q(".selected-file")), "Sample selected");
  await ui.click("Upload XML");
  await waitFor(() => !ui.q(".selected-file"), "Selection cleared");
  assert.equal(ui.button("Review work order").disabled, true);
  assert.equal(
    ui.q('[role="tab"][aria-selected="true"]').textContent,
    "Upload XML",
  );
  await ui.upload("hello", "notes.txt");
  assert.ok(ui.q(".error-banner"));
  await ui.click("Cloud Storage");
  await waitFor(() => !ui.q(".error-banner"), "Error cleared by tab switch");
  assert.equal(ui.qa('.sample-card[aria-pressed="true"]').length, 0);
});

test("utilisation bounds keep Analyse disabled and never blank the app", async (t) => {
  const ui = await app(t);
  await ui.sample("Fuel-injection nozzle");
  const expectEnabled = (value, enabled) => {
    ui.setCycles(value);
    return waitFor(
      () => ui.analyseEnabled() === enabled && ui.q("#cycles").value === value,
      `Utilisation ${value} -> ${enabled ? "enabled" : "disabled"}`,
    );
  };
  // Alternate with a valid value so each disabled state is a real transition.
  await expectEnabled("24", true);
  await expectEnabled("25", false);
  await expectEnabled("24", true);
  await expectEnabled("1e-9", false); // used to blank the whole app
  assert.match(ui.body(), /Expected flight cycles per day/);
  await expectEnabled("24", true);
  await expectEnabled("0.05", false);
  await expectEnabled("1", true);
  await ui.click("Analyse component");
  await ui.waitForResult();
  assert.match(ui.body(), /Assuming 1 flight cycle \/ day/);
  assert.doesNotMatch(ui.body(), /Assuming 1 flight cycles/);
  assert.match(ui.text(".cycle-band-caption"), /window assumes 1 flight cycle \/ day$/);
});

test("the calendar window follows utilisation and Start again restores the default", async (t) => {
  const ui = await app(t);
  await ui.sample("Fuel-injection nozzle");
  assert.equal(ui.q("#cycles").value, "6");
  ui.setCycles("2");
  await waitFor(() => ui.q("#cycles").value === "2", "Utilisation set to 2");
  await ui.click("Analyse component");
  await ui.waitForResult();
  assert.match(ui.text(".date-metric"), /22 Dec 2026\s*[–-]\s*31 Jan 2027/);
  assert.match(ui.body(), /Assuming 2 flight cycles \/ day/);
  await ui.click("Start again");
  await ui.sample("Fuel-injection nozzle");
  assert.equal(ui.q("#cycles").value, "6");
});

test("cancellation prevents a stale forecast from ever landing", async (t) => {
  const ui = await app(t);
  await ui.sample("Fuel-injection nozzle");
  await ui.click("Analyse component");
  await waitFor(() => ui.currentStep().includes("Analyse"), "Analysing");
  await ui.click("Cancel analysis");
  await waitFor(
    () => ui.currentStep().includes("Review component"),
    "Back on review",
  );
  // Deliberate: wait past the full 2600ms mock pipeline so a leaked run would land.
  await pause(PAST_PIPELINE_MS);
  assert.ok(
    ui.currentStep().includes("Review component"),
    `Stepper moved after cancel: ${ui.currentStep()}`,
  );
  assert.equal(ui.q(".result-panel"), null);
  assert.doesNotMatch(ui.body(), /ILLUSTRATIVE FORECAST|180–260/);
  assert.equal(ui.analyseEnabled(), true);
  // The next scenario still gets its own outcome.
  await ui.click("Start again");
  await ui.sample("Water boiler");
  await ui.click("Analyse component");
  await ui.waitForResult();
  assert.match(ui.body(), /ESTIMATE UNAVAILABLE/);
  assert.match(ui.body(), /Water boiler/);
  assert.doesNotMatch(ui.body(), /180–260/);
});

test("Start again during analysis prevents a stale run from advancing the journey", async (t) => {
  const ui = await app(t);
  await ui.sample("Fuel-injection nozzle");
  await ui.click("Analyse component");
  await waitFor(() => ui.currentStep().includes("Analyse"), "Analysing");
  await ui.click("Start again");
  await waitFor(
    () => ui.currentStep().includes("Select work order"),
    "Back at the start",
  );
  // Deliberate: wait past the full 2600ms mock pipeline so a leaked run would land.
  // reset() nulls the document, so a leak renders no forecast text; only the
  // stepper reveals it by jumping to "Replacement outlook".
  await pause(PAST_PIPELINE_MS);
  assert.ok(
    ui.currentStep().includes("Select work order"),
    `Stepper moved after Start again: ${ui.currentStep()}`,
  );
  assert.equal(ui.q(".result-panel"), null);
  assert.doesNotMatch(ui.body(), /ILLUSTRATIVE FORECAST|180–260/);
  assert.ok(ui.button("Choose XML file") || ui.button("Review work order"));
});

test("architecture view discloses planned connections and preserves the journey", async (t) => {
  const ui = await app(t);
  await ui.sample("Convection oven");
  await ui.click("How it works");
  await waitFor(() => /Target architecture/.test(ui.body()), "Architecture");
  assert.match(ui.body(), /backend is being developed independently/);
  await ui.click("Explore the demo");
  await waitFor(() => /DEMO-OVEN-001/.test(ui.body()), "Journey restored");
  assert.equal(ui.button("Analyse component").disabled, false);
});
