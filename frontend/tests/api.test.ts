import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import {
  UNREACHABLE_MESSAGE,
  analyzeXml,
  buildAnalyzeUrl,
  formatConfidence,
  formatHeadline,
  formatStatus,
  mapAnalysis,
} from "../src/data/api.ts";
import type { RecommendationOutlook } from "../src/domain.ts";

const fixture = (name: string) =>
  JSON.parse(
    readFileSync(new URL(`./fixtures/analysis/${name}`, import.meta.url), "utf8"),
  );
const landing = fixture("landing_light_rh.json");
const aft = fixture("aft_smoke_detector.json");

function withPma(pma: Record<string, unknown>) {
  return { ...landing, pma: { ...landing.pma, recommendation: null, ...pma } };
}
function rec(overrides: Partial<RecommendationOutlook> = {}) {
  const base = mapAnalysis(landing, 6);
  assert.equal(base.status, "recommendation");
  return { ...(base as RecommendationOutlook), ...overrides };
}

test("real landing-light response maps to a recommendation", () => {
  const o = mapAnalysis(landing, 6);
  assert.deepEqual(o, {
    status: "recommendation",
    componentKey: "72605363-1|RH",
    action: "recommend_inspection_or_part_planning",
    basis: "similar_workorders",
    tac: { p50: 14704, p90: 14984, p95: 15015 },
    lead: { p50: 188, p90: 468, p95: 499 },
    referenceTac: 14516,
    confidence: {
      level: "medium",
      similarity: 0.8800903231906029,
      sampleSize: 5,
      cv: 0.6654579462814479,
    },
    evidence: [
      { woId: "190599369", sim: 0.9036652113644 },
      { woId: "55702121", sim: 0.8789465448833256 },
      { woId: "190362777", sim: 0.8784223280775195 },
    ],
    cyclesPerDay: 6,
    asOf: "2026-09-24",
    limitations: landing.limitations,
  });
});

test("real AFT smoke detector response maps to a recommendation", () => {
  const o = mapAnalysis(aft, 4) as RecommendationOutlook;
  assert.equal(o.status, "recommendation");
  assert.equal(o.componentKey, "473597-5|AFT");
  assert.deepEqual(o.tac, { p50: 19073, p90: 19859, p95: 20187 });
  assert.equal(o.referenceTac, 18660);
  assert.equal(o.cyclesPerDay, 4);
});

test("historical interval without a recommendation maps to interval", () => {
  const o = mapAnalysis(
    withPma({
      decision: "historical_interval",
      reason: null,
      component_key: "X|L",
      interval: { p50: 812.4, p90: 1500.6, n: 9, aircraft: 4 },
    }),
    6,
  );
  assert.deepEqual(o, {
    status: "interval",
    componentKey: "X|L",
    p50: 812,
    p90: 1501,
    n: 9,
    aircraft: 4,
    asOf: "2026-09-24",
    limitations: landing.limitations,
  });
});

test("no prediction maps to unavailable with readable reason", () => {
  const o = mapAnalysis(
    withPma({
      decision: "out_of_scope",
      reason: "component_not_in_focus_set",
      missing: ["focus_component"],
    }),
    6,
  );
  assert.equal(o.status, "unavailable");
  if (o.status !== "unavailable") return;
  assert.match(o.reason, /not one of the tracked focus components/);
  assert.deepEqual(o.missing, ["focus_component"]);
  assert.deepEqual(o.limitations, landing.limitations);
});

test("unknown reason code falls back to the raw code", () => {
  const o = mapAnalysis(withPma({ reason: "brand_new_code" }), 6);
  assert.equal(o.status === "unavailable" && o.reason, "brand_new_code");
});

test("malformed response is unavailable, not a crash", () => {
  assert.equal(mapAnalysis(null, 6).status, "unavailable");
  assert.equal(mapAnalysis({}, 6).status, "unavailable");
});

test("headline and status text match the chat wording", () => {
  const o = rec();
  assert.equal(
    formatHeadline(o),
    "Replace around TAC 14,704 (p50) · 14,984 (p90) · 15,015 (p95)",
  );
  assert.equal(formatStatus(o), "Latest known TAC 14,516 → 188 cycles before p50");
  assert.equal(formatConfidence(o), "n=5 · CV 0.67 · similarity 0.88");
});

test("headline omits p95 when absent", () => {
  const o = rec({ tac: { p50: 14704, p90: 14984, p95: null } });
  assert.equal(formatHeadline(o), "Replace around TAC 14,704 (p50) · 14,984 (p90)");
});

test("headline falls back to lead cycles when no TAC anchor", () => {
  const o = rec({ tac: { p50: null, p90: null, p95: null }, referenceTac: null });
  assert.equal(
    formatHeadline(o),
    "Replace within 188 (p50) · 468 (p90) · 499 (p95) flight cycles",
  );
  assert.equal(formatStatus(o), null);
});

test("status reports overdue positions", () => {
  assert.equal(formatStatus(rec({ referenceTac: 14990 })), "Latest known TAC 14,990 → past p90");
  assert.equal(
    formatStatus(rec({ referenceTac: 15100 })),
    "Latest known TAC 15,100 → already past p95",
  );
  assert.equal(formatStatus(rec({ referenceTac: 14800 })), "Latest known TAC 14,800 → past p50");
});

test("confidence omits missing CV and similarity", () => {
  const o = rec({ confidence: { level: "low", similarity: null, sampleSize: 3, cv: null } });
  assert.equal(formatConfidence(o), "n=3");
});

test("URL uses mode from work-order status and selects only among many", () => {
  const now = new Date("2026-09-24T13:00:00.000Z");
  const open = buildAnalyzeUrl("/api", { status: "Open work order", id: "EX-1" }, 1, now);
  assert.equal(
    open,
    "/api/workorders/analyze?mode=new_work_order&analysis_as_of=2026-09-24T13%3A00%3A00.000Z",
  );
  const closed = buildAnalyzeUrl(
    "/api",
    { status: "Closed · historical review", id: "EX 2" },
    2,
    now,
  );
  assert.match(closed, /mode=historical_replay/);
  assert.match(closed, /selected_wo_id=EX\+2/);
});

function fakeFetch(respond: () => Promise<Response>) {
  const calls: { url: string; init?: RequestInit }[] = [];
  const impl = ((url: string, init?: RequestInit) => {
    calls.push({ url, init });
    return respond();
  }) as typeof fetch;
  return { impl, calls };
}
const json = (status: number, body: unknown) =>
  Promise.resolve(new Response(JSON.stringify(body), { status }));

test("analyzeXml posts the XML and returns JSON", async () => {
  const { impl, calls } = fakeFetch(() => json(200, landing));
  const out = await analyzeXml("<x/>", "/api/u", new AbortController().signal, impl);
  assert.equal((out as { wo_id: string }).wo_id, landing.wo_id);
  assert.equal(calls[0].init?.method, "POST");
  assert.equal(calls[0].init?.body, "<x/>");
});

test("analyzeXml surfaces backend 422 message", async () => {
  const { impl } = fakeFetch(() =>
    json(422, { detail: { code: "invalid_xml", message: "Upload is not valid XML" } }),
  );
  await assert.rejects(
    analyzeXml("<x/>", "/u", new AbortController().signal, impl),
    { message: "Upload is not valid XML" },
  );
});

test("analyzeXml maps 503 to history unavailable", async () => {
  const { impl } = fakeFetch(() => json(503, { detail: { code: "x", message: "y" } }));
  await assert.rejects(analyzeXml("<x/>", "/u", new AbortController().signal, impl), {
    message: /History service unavailable/,
  });
});

test("analyzeXml maps network failure and proxy 502 to unreachable", async () => {
  const down = fakeFetch(() => Promise.reject(new TypeError("fetch failed")));
  await assert.rejects(analyzeXml("<x/>", "/u", new AbortController().signal, down.impl), {
    message: UNREACHABLE_MESSAGE,
  });
  const proxy = fakeFetch(() => Promise.resolve(new Response("", { status: 502 })));
  await assert.rejects(analyzeXml("<x/>", "/u", new AbortController().signal, proxy.impl), {
    message: UNREACHABLE_MESSAGE,
  });
});

test("analyzeXml propagates abort as AbortError", async () => {
  const controller = new AbortController();
  const { impl } = fakeFetch(() => {
    controller.abort();
    return Promise.reject(new DOMException("Aborted", "AbortError"));
  });
  await assert.rejects(analyzeXml("<x/>", "/u", controller.signal, impl), {
    name: "AbortError",
  });
});
