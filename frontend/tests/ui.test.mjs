import assert from "node:assert/strict";
import { readFileSync, readdirSync } from "node:fs";
import { fileURLToPath } from "node:url";
import test from "node:test";
import { JSDOM } from "jsdom";

// Exercises the built React app without a server. This checks interactions and
// content; Playwright remains responsible for real-browser rendering, layout,
// focus and overflow. The analyze endpoint is stubbed with captured backend
// responses, so no backend is needed.
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
const sampleXml = (name) => fixture(`../public/samples/${name}`);
const analysisFixture = (name) =>
  JSON.parse(localFixture(`analysis/${name}.json`));

const LANDING = analysisFixture("landing_light_rh");
const SMOKE = analysisFixture("aft_smoke_detector");
/** A captured response with its PMA block replaced: no recommendation. */
const withPma = (pma) => ({
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

const pause = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
async function waitFor(predicate, message = "Expected UI state") {
  const deadline = Date.now() + 6500;
  while (Date.now() < deadline) {
    if (predicate()) return;
    await pause(20);
  }
  assert.fail(message);
}

/** Minimal Response stand-in: the app only reads ok, status, json() and text(). */
const reply = (status, body) => ({
  ok: status >= 200 && status < 300,
  status,
  json: async () =>
    typeof body === "string" ? JSON.parse(body) : structuredClone(body),
  text: async () => (typeof body === "string" ? body : JSON.stringify(body)),
});

/** Installs window.fetch: samples come from public/samples, the analyze call
 * answers with `api.respond(...)` (default: the landing-light fixture). */
function installFetch(window) {
  let next = { status: 200, body: LANDING };
  let held = null;
  const api = {
    requests: [],
    respond(body, status = 200) {
      next = { status, body };
    },
    unreachable() {
      next = { reject: true };
    },
    /** Holds analyze responses until the returned release() is called. The
     * held response deliberately ignores the abort signal, so a leaked run
     * has every chance to land after a cancel. */
    hold() {
      let release;
      held = new Promise((resolve) => (release = resolve));
      return () => {
        release();
        held = null;
      };
    },
  };
  window.fetch = async (input, init = {}) => {
    const url = new URL(String(input), window.location.href);
    const sample = url.pathname.match(/^\/samples\/([\w.-]+\.xml)$/);
    if (sample) return reply(200, sampleXml(sample[1]));
    if (!url.pathname.endsWith("/api/workorders/analyze"))
      return reply(404, "Not found");
    const request = {
      method: init.method,
      body: init.body,
      params: url.searchParams,
      signal: init.signal,
    };
    api.requests.push(request);
    const outcome = next;
    if (held) await held;
    if (outcome.reject) throw new TypeError("Failed to fetch");
    return reply(outcome.status, outcome.body);
  };
  return api;
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
  const api = installFetch(window);
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
  /** Resolves once the app has settled a failed analysis back on review. */
  const waitForError = () =>
    waitFor(
      () =>
        Boolean(q(".error-banner")) &&
        currentStep().includes("Review component"),
      "Error shown on review",
    );
  return {
    window,
    api,
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
    waitForError,
  };
}

test("landing-light sample posts its XML and renders the live recommendation", async (t) => {
  const ui = await app(t);
  assert.equal(ui.button("Review work order").disabled, true);
  await ui.sample("RH landing light power supply");
  assert.match(ui.body(), /Work order EX-LLT-001/);
  assert.match(ui.text(".review-panel .facts"), /9H-REG00427/);
  // I4: no issue TAC in this order, and aircraft cycles are never component age.
  assert.equal(ui.fact("Aircraft cycles at issue"), "Not recorded");
  assert.match(ui.body(), /Aircraft total cycles are not component age/);
  assert.equal(ui.analyseEnabled(), true, "single component auto-selected");
  await ui.click("Analyse component");
  await ui.waitForResult();

  assert.equal(ui.api.requests.length, 1);
  const [request] = ui.api.requests;
  assert.equal(request.method, "POST");
  assert.equal(request.body, sampleXml("landing_light_rh.xml"));
  assert.equal(request.params.get("mode"), "new_work_order");
  assert.ok(request.params.get("analysis_as_of"), "analysis_as_of sent");
  assert.equal(request.params.has("selected_wo_id"), false);

  assert.match(ui.body(), /RECOMMENDATION/);
  assert.equal(
    ui.text("h4.rec-title"),
    "Plan inspection / part replacement — 72605363-1|RH",
  );
  assert.equal(
    ui.text(".rec-headline"),
    "Replace around TAC 14,704 (p50) · 14,984 (p90) · 15,015 (p95)",
  );
  assert.equal(
    ui.text(".rec-status"),
    "Latest known TAC 14,516 → 188 cycles before p50",
  );
  assert.match(ui.text(".rec-confidence"), /MEDIUM CONFIDENCE/);
  assert.match(ui.text(".rec-confidence"), /n=5 · CV 0\.67 · similarity 0\.88/);
  assert.match(ui.body(), /Basis: similar work orders \+ lead-time history/);
  assert.match(ui.body(), /Heuristic estimate, not a calibrated forecast\./);
  assert.match(ui.body(), /WO 190599369/);
  assert.match(ui.body(), /similarity 0\.90/);
  assert.match(ui.body(), /Example work order · Live PMA analysis/);
  assert.equal(ui.text(".result-identity h3"), "72605363-1|RH");
  // Each citation [N] in the basis line points at the matching WO entry.
  const cites = ui.qa(".rec-basis a.rec-cite");
  const woIds = ["190599369", "55702121", "190362777"];
  assert.deepEqual(
    cites.map((a) => a.textContent),
    ["[1]", "[2]", "[3]"],
  );
  woIds.forEach((woId, i) => {
    assert.equal(cites[i].getAttribute("href"), `#rec-evidence-${i + 1}`);
    assert.equal(
      ui.text(`ul.rec-evidence li#rec-evidence-${i + 1} strong`),
      `[${i + 1}] WO ${woId}`,
    );
  });
  // Calendar window at the default 6 cycles / day from analysis_as_of 2026-09-24.
  assert.match(ui.body(), /26 Oct\s*[–-]\s*11 Dec 2026/);
  assert.match(ui.body(), /Assuming 6 flight cycles \/ day/);
  assert.doesNotMatch(
    ui.body(),
    /ILLUSTRATIVE FORECAST|HISTORICAL INTERVAL|ESTIMATE UNAVAILABLE|NaN|undefined/,
  );
  assert.ok(ui.currentStep().includes("Replacement outlook"));
  await ui.click("Analyse another work order");
  await waitFor(
    () => ui.currentStep().includes("Select work order"),
    "Returned to the start",
  );
  assert.equal(ui.button("Review work order").disabled, true);
});

test("smoke-detector sample renders its own recommendation", async (t) => {
  const ui = await app(t);
  ui.api.respond(SMOKE);
  await ui.sample("AFT cargo smoke detector");
  assert.match(ui.body(), /Work order MANUAL-AFT-PN-001/);
  await ui.click("Analyse component");
  await ui.waitForResult();
  assert.equal(ui.api.requests[0].body, sampleXml("aft_smoke_detector.xml"));
  assert.equal(
    ui.text("h4.rec-title"),
    "Plan inspection / part replacement — 473597-5|AFT",
  );
  assert.equal(
    ui.text(".rec-headline"),
    "Replace around TAC 19,073 (p50) · 19,859 (p90) · 20,187 (p95)",
  );
  assert.equal(
    ui.text(".rec-status"),
    "Latest known TAC 18,660 → 413 cycles before p50",
  );
  assert.match(ui.text(".rec-confidence"), /n=16 · CV 1\.10 · similarity 0\.74/);
  assert.match(ui.body(), /WO 190736258/);
  assert.doesNotMatch(ui.body(), /14,704|72605363-1\|RH/);
});

test("a historical interval is labelled as observed, not forecast", async (t) => {
  const ui = await app(t);
  ui.api.respond(INTERVAL);
  await ui.sample("RH landing light power supply");
  await ui.click("Analyse component");
  await ui.waitForResult();
  assert.match(ui.body(), /HISTORICAL INTERVAL/);
  assert.match(ui.body(), /Observed historical interval \(not a forecast\)/);
  assert.match(ui.body(), /p50 300 cycles · p90 900 cycles \(n=13, 10 aircraft\)/);
  assert.ok(!ui.q(".rec-confidence"), "no confidence block for an interval");
  assert.doesNotMatch(
    ui.body(),
    /RECOMMENDATION|ESTIMATE UNAVAILABLE|Replace around TAC|NaN/,
  );
});

test("an unavailable estimate shows the reason and what is missing", async (t) => {
  const ui = await app(t);
  ui.api.respond(UNAVAILABLE);
  await ui.sample("RH landing light power supply");
  await ui.click("Analyse component");
  await ui.waitForResult();
  assert.match(ui.body(), /ESTIMATE UNAVAILABLE/);
  assert.match(
    ui.body(),
    /This component is not one of the tracked focus components\./,
  );
  assert.match(ui.body(), /Verified component installation date/);
  assert.match(ui.body(), /Current aircraft TAC was not supplied/);
  assert.ok(!ui.q(".rec-headline"), "no headline without an estimate");
  assert.doesNotMatch(ui.body(), /RECOMMENDATION|HISTORICAL INTERVAL|NaN/);
});

test("an unreachable backend returns to review with the start-up hint", async (t) => {
  const ui = await app(t);
  ui.api.unreachable();
  await ui.sample("RH landing light power supply");
  await ui.click("Analyse component");
  await ui.waitForError();
  assert.equal(ui.text(".error-banner span"), UNREACHABLE);
  assert.ok(!ui.q(".result-panel"), ".result-panel absent");
  assert.equal(ui.analyseEnabled(), true);
  // Once the backend is up, the same review can be analysed again.
  ui.api.respond(LANDING);
  await ui.click("Analyse component");
  await ui.waitForResult();
  assert.ok(!ui.q(".error-banner"), ".error-banner absent");
  assert.match(ui.text(".rec-headline"), /14,704/);
});

test("a 422 shows the backend's own message", async (t) => {
  const ui = await app(t);
  ui.api.respond(
    { detail: { message: "The uploaded XML has no work order." } },
    422,
  );
  await ui.sample("RH landing light power supply");
  await ui.click("Analyse component");
  await ui.waitForError();
  assert.equal(
    ui.text(".error-banner span"),
    "The uploaded XML has no work order.",
  );
  assert.ok(!ui.q(".result-panel"), ".result-panel absent");
});

test("Cloud Storage lists both real samples and marks the chosen one", async (t) => {
  const ui = await app(t);
  await ui.click("Cloud Storage");
  await waitFor(() => ui.qa(".sample-card").length === 2, "Two samples");
  const list = ui.text(".sample-list");
  assert.match(list, /RH landing light power supply/);
  assert.match(list, /PN 72605363-1/);
  assert.match(list, /AFT cargo smoke detector/);
  assert.match(list, /PN 473597-5/);
  assert.doesNotMatch(list, /synthetic/i);
  await ui.click("AFT cargo smoke detector");
  await waitFor(
    () =>
      ui.q('.sample-card[aria-pressed="true"]')?.textContent.includes(
        "AFT cargo smoke detector",
      ),
    "Chosen card pressed",
  );
  assert.equal(ui.qa('.sample-card[aria-pressed="true"]').length, 1);
});

test("an uploaded sample XML is posted as-is and labelled as an upload", async (t) => {
  const ui = await app(t);
  const xml = sampleXml("landing_light_rh.xml");
  await ui.upload(xml, "my_order.xml");
  await ui.click("Review work order");
  assert.match(ui.body(), /Work order EX-LLT-001/);
  await ui.click("Analyse component");
  await ui.waitForResult();
  assert.equal(ui.api.requests[0].body, xml);
  assert.equal(ui.api.requests[0].params.get("mode"), "new_work_order");
  assert.match(ui.body(), /Uploaded XML · Live PMA analysis/);
  assert.doesNotMatch(ui.body(), /Example work order · Live PMA analysis/);
});

test("uploaded closed XML keeps its identity and is replayed historically", async (t) => {
  const ui = await app(t);
  ui.api.respond(UNAVAILABLE);
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
  assert.equal(ui.api.requests[0].params.get("mode"), "historical_replay");
  assert.equal(ui.api.requests[0].params.has("selected_wo_id"), false);
  assert.match(ui.body(), /ESTIMATE UNAVAILABLE/);
  assert.match(ui.body(), /Uploaded XML · Live PMA analysis/);
  // I5 on the results screen.
  assert.match(ui.text(".result-identity .eyebrow"), /Closed · historical review/);
  assert.match(ui.text(".result-panel"), /closed work order/);
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

test("multi-order XML sends the selected work order id", async (t) => {
  const ui = await app(t);
  const xml = localFixture("jsdom-two_workorders_distinct.xml");
  await ui.upload(xml);
  await ui.click("Review work order");
  ui.chooseOrder(1);
  await waitFor(() => ui.q("select").value === "1", "Order B selected");
  await ui.chooseComponent(1);
  await ui.click("Analyse component");
  await ui.waitForResult();
  const [request] = ui.api.requests;
  assert.equal(request.body, xml);
  assert.equal(request.params.get("selected_wo_id"), "JSDOM-ORDER-B");
  assert.equal(request.params.get("mode"), "new_work_order");
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
  assert.equal(ui.api.requests.length, 0);
});

test("a CDATA section that mentions a doctype is accepted", async (t) => {
  const ui = await app(t);
  await ui.upload(localFixture("jsdom-cdata_doctype_mention.xml"));
  assert.ok(!ui.q(".error-banner"), ".error-banner absent");
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
  await ui.click("RH landing light power supply");
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
  await ui.sample("RH landing light power supply");
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
  assert.match(ui.body(), /31 Mar 2027\s*[–-]\s*5 Jan 2028/);
});

test("the calendar window follows utilisation and Start again restores the default", async (t) => {
  const ui = await app(t);
  await ui.sample("RH landing light power supply");
  assert.equal(ui.q("#cycles").value, "6");
  ui.setCycles("2");
  await waitFor(() => ui.q("#cycles").value === "2", "Utilisation set to 2");
  await ui.click("Analyse component");
  await ui.waitForResult();
  // 188 / 2 = 94 days and 468 / 2 = 234 days from 2026-09-24.
  assert.match(ui.body(), /27 Dec 2026\s*[–-]\s*16 May 2027/);
  assert.match(ui.body(), /Assuming 2 flight cycles \/ day/);
  await ui.click("Start again");
  await ui.sample("RH landing light power supply");
  assert.equal(ui.q("#cycles").value, "6");
});

test("cancellation prevents a late response from ever landing", async (t) => {
  const ui = await app(t);
  await ui.sample("RH landing light power supply");
  const release = ui.api.hold();
  await ui.click("Analyse component");
  await waitFor(() => ui.currentStep().includes("Analyse"), "Analysing");
  await waitFor(() => ui.api.requests.length === 1, "Request sent");
  await ui.click("Cancel analysis");
  await waitFor(
    () => ui.currentStep().includes("Review component"),
    "Back on review",
  );
  assert.equal(ui.api.requests[0].signal.aborted, true, "request aborted");
  // Deliberate: let the held response land, then give it time to render.
  release();
  await pause(200);
  assert.ok(
    ui.currentStep().includes("Review component"),
    `Stepper moved after cancel: ${ui.currentStep()}`,
  );
  assert.ok(!ui.q(".result-panel"), ".result-panel absent");
  assert.ok(!ui.q(".error-banner"), "an abort is not an error");
  assert.doesNotMatch(ui.body(), /RECOMMENDATION|14,704/);
  assert.equal(ui.analyseEnabled(), true);
  // The next scenario still gets its own outcome.
  ui.api.respond(SMOKE);
  await ui.click("Start again");
  await ui.sample("AFT cargo smoke detector");
  await ui.click("Analyse component");
  await ui.waitForResult();
  assert.match(ui.text(".rec-headline"), /19,073/);
  assert.doesNotMatch(ui.body(), /14,704/);
});

test("Start again during analysis prevents a stale run from advancing the journey", async (t) => {
  const ui = await app(t);
  await ui.sample("RH landing light power supply");
  const release = ui.api.hold();
  await ui.click("Analyse component");
  await waitFor(() => ui.currentStep().includes("Analyse"), "Analysing");
  await waitFor(() => ui.api.requests.length === 1, "Request sent");
  await ui.click("Start again");
  await waitFor(
    () => ui.currentStep().includes("Select work order"),
    "Back at the start",
  );
  // Deliberate: let the held response land. reset() nulls the document, so a
  // leak renders no result text; only the stepper reveals it by jumping to
  // "Replacement outlook".
  release();
  await pause(200);
  assert.ok(
    ui.currentStep().includes("Select work order"),
    `Stepper moved after Start again: ${ui.currentStep()}`,
  );
  assert.ok(!ui.q(".result-panel"), ".result-panel absent");
  assert.ok(!ui.q(".error-banner"), ".error-banner absent");
  assert.doesNotMatch(ui.body(), /RECOMMENDATION|14,704/);
  assert.ok(ui.button("Choose XML file") || ui.button("Review work order"));
});

test("architecture view preserves the journey", async (t) => {
  const ui = await app(t);
  await ui.sample("AFT cargo smoke detector");
  await ui.click("How it works");
  await waitFor(() => /Target architecture/.test(ui.body()), "Architecture");
  await ui.click("Explore the demo");
  await waitFor(() => /MANUAL-AFT-PN-001/.test(ui.body()), "Journey restored");
  assert.equal(ui.button("Analyse component").disabled, false);
});
