import type { Outlook, Quantiles, RecommendationOutlook } from "../domain.ts";

/** Pure adapter for POST /workorders/analyze. Only erasable TypeScript here:
 * `node --test tests/api.test.ts` runs this file without a build step. */

export const API_BASE: string =
  (import.meta as { env?: Record<string, string | undefined> }).env
    ?.VITE_API_BASE ?? "/api";

export const UNREACHABLE_MESSAGE =
  "Analysis service unreachable. Start the backend: PMA_PREDICTION_ENABLED=true uv run uvicorn pm_agent.fast_api_app:app --port 8000";

// Wording mirrors pm_agent/workorders/chat.py so the UI and chat reply agree.
const REASON_TEXT: Record<string, string> = {
  empty_text: "No usable work-order text or part number was found.",
  embedding_failed: "The work-order text could not be embedded.",
  embedding_incompatible:
    "The embedding was not compatible with the reference space.",
  no_confident_component_match:
    "No focus component could be matched with confidence.",
  ambiguous_position: "The matched component's position could not be resolved.",
  component_not_in_focus_set:
    "This component is not one of the tracked focus components.",
  position_not_in_focus_set: "This component's position is not tracked.",
  aircraft_type_not_in_scope: "This aircraft type is not in scope.",
  no_lead_time_samples:
    "No historical lead-time samples exist for this component.",
  insufficient_samples: "Too few historical samples exist for this component.",
  samples_not_symptom_to_replacement:
    "Past samples are mostly intervals between consecutive replacements, not symptom-to-replacement lead times.",
  prediction_disabled:
    "PMA prediction is disabled on the backend (set PMA_PREDICTION_ENABLED=true).",
  data_source_unavailable:
    "The prediction data source is currently unavailable.",
};

const ACTION_TEXT: Record<string, string> = {
  recommend_inspection_or_part_planning: "Plan inspection / part replacement",
  monitor: "Monitor",
};

const BASIS_TEXT: Record<string, string> = {
  similar_workorders: "similar work orders + lead-time history",
  component_history: "component lead-time history",
  fleet_replacement_interval: "fleet replacement pattern",
};

export const actionText = (action: string) => ACTION_TEXT[action] ?? action;
export const basisText = (basis: string) => BASIS_TEXT[basis] ?? basis;

type Json = Record<string, unknown>;
const obj = (value: unknown): Json =>
  value && typeof value === "object" && !Array.isArray(value)
    ? (value as Json)
    : {};
const num = (value: unknown): number | null =>
  typeof value === "number" && Number.isFinite(value) ? value : null;
const int = (value: unknown): number | null => {
  const n = num(value);
  return n === null ? null : Math.round(n);
};
const strings = (value: unknown): string[] =>
  Array.isArray(value)
    ? value.filter((item): item is string => typeof item === "string")
    : [];

export function buildAnalyzeUrl(
  base: string,
  order: { status: string; id: string },
  orderCount: number,
  now: Date,
): string {
  const params = new URLSearchParams({
    mode: order.status.startsWith("Closed")
      ? "historical_replay"
      : "new_work_order",
    analysis_as_of: now.toISOString(),
  });
  if (orderCount > 1) params.set("selected_wo_id", order.id);
  return `${base}/workorders/analyze?${params}`;
}

export function mapAnalysis(response: unknown, cyclesPerDay: number): Outlook {
  const root = obj(response);
  const pma = obj(root.pma);
  const limitations = strings(root.limitations);
  const asOf = String(root.analysis_as_of ?? "").slice(0, 10);
  const rec = obj(pma.recommendation);
  if (typeof rec.component_key === "string") {
    const predicted = obj(rec.predicted_replacement);
    const confidence = obj(rec.confidence);
    const quantiles = (prefix: string): Quantiles => ({
      p50: int(predicted[`${prefix}_p50`]),
      p90: int(predicted[`${prefix}_p90`]),
      p95: int(predicted[`${prefix}_p95`]),
    });
    return {
      status: "recommendation",
      componentKey: rec.component_key,
      action: String(rec.action ?? ""),
      basis: String(rec.basis ?? ""),
      tac: quantiles("tac"),
      lead: quantiles("lead_tac"),
      referenceTac: int(rec.reference_tac),
      confidence: {
        level: String(confidence.level ?? "unknown"),
        similarity: num(confidence.similarity),
        sampleSize: int(confidence.sample_size) ?? 0,
        cv: num(confidence.cv),
      },
      evidence: (Array.isArray(rec.evidence) ? rec.evidence : []).map(
        (item) => ({ woId: String(obj(item).wo_id ?? ""), sim: num(obj(item).sim) }),
      ),
      cyclesPerDay,
      asOf,
      limitations,
    };
  }
  const interval = obj(pma.interval);
  const p50 = int(interval.p50);
  const p90 = int(interval.p90);
  if (pma.decision === "historical_interval" && p50 !== null && p90 !== null)
    return {
      status: "interval",
      componentKey:
        typeof pma.component_key === "string" ? pma.component_key : null,
      p50,
      p90,
      n: int(interval.n) ?? 0,
      aircraft: int(interval.aircraft),
      asOf,
      limitations,
    };
  const reason = typeof pma.reason === "string" ? pma.reason : "";
  return {
    status: "unavailable",
    reason: REASON_TEXT[reason] ?? (reason || "The analysis returned no prediction."),
    missing: strings(pma.missing),
    limitations,
  };
}

const fmt = (n: number) => n.toLocaleString("en-GB");

export function formatHeadline(o: RecommendationOutlook): string {
  const useTac = o.tac.p50 !== null && o.tac.p90 !== null;
  const q = useTac ? o.tac : o.lead;
  const parts: string[] = [];
  if (q.p50 !== null) parts.push(`${fmt(q.p50)} (p50)`);
  if (q.p90 !== null) parts.push(`${fmt(q.p90)} (p90)`);
  if (q.p95 !== null) parts.push(`${fmt(q.p95)} (p95)`);
  if (!parts.length) return "Replacement timing unavailable";
  return useTac
    ? `Replace around TAC ${parts.join(" · ")}`
    : `Replace within ${parts.join(" · ")} flight cycles`;
}

export function formatStatus(o: RecommendationOutlook): string | null {
  const ref = o.referenceTac;
  if (ref === null || o.tac.p50 === null) return null;
  const prefix = `Latest known TAC ${fmt(ref)} → `;
  if (o.tac.p95 !== null && ref >= o.tac.p95) return `${prefix}already past p95`;
  if (o.tac.p90 !== null && ref >= o.tac.p90) return `${prefix}past p90`;
  if (ref >= o.tac.p50) return `${prefix}past p50`;
  const lead = o.lead.p50 ?? o.tac.p50 - ref;
  return `${prefix}${fmt(lead)} cycles before p50`;
}

export function formatConfidence(o: RecommendationOutlook): string {
  const parts = [`n=${o.confidence.sampleSize}`];
  if (o.confidence.cv !== null) parts.push(`CV ${o.confidence.cv.toFixed(2)}`);
  if (o.confidence.similarity !== null)
    parts.push(`similarity ${o.confidence.similarity.toFixed(2)}`);
  return parts.join(" · ");
}

export async function analyzeXml(
  xml: string,
  url: string,
  signal: AbortSignal,
  fetchImpl: typeof fetch = fetch,
): Promise<unknown> {
  let response: Response;
  try {
    response = await fetchImpl(url, {
      method: "POST",
      headers: { "Content-Type": "application/xml" },
      body: xml,
      signal,
    });
  } catch (error) {
    if (signal.aborted || (error as { name?: string })?.name === "AbortError")
      throw error;
    throw new Error(UNREACHABLE_MESSAGE);
  }
  if (response.ok) return response.json();
  if (response.status === 502 || response.status === 504)
    throw new Error(UNREACHABLE_MESSAGE);
  if (response.status === 503)
    throw new Error(
      "History service unavailable. Check the backend's BigQuery configuration and credentials.",
    );
  let message = `Analysis failed (HTTP ${response.status}).`;
  try {
    const detail = obj(obj(await response.json()).detail);
    if (typeof detail.message === "string") message = detail.message;
  } catch {
    // Non-JSON error body: keep the generic message.
  }
  throw new Error(message);
}
