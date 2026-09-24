/** UI-owned contract. Translate backend responses into these types in an adapter. */
export type Source = "upload" | "sample";
export interface Component {
  id: string;
  partNumber: string;
  description: string;
  serial: string | null;
  position: string | null;
  role: "recorded" | "installed" | "removed";
}
export interface WorkOrder {
  id: string;
  aircraft: string | null;
  aircraftType: string | null;
  status: string;
  issuedAt: string | null;
  issueCycles: string | null;
  symptoms: string;
  components: Component[];
}
export interface WorkOrderDocument {
  filename: string;
  source: Source;
  sampleId?: string;
  /** The exact XML text, posted unchanged to the analysis service. */
  xml: string;
  orders: WorkOrder[];
}
export interface Sample {
  id: string;
  title: string;
  partNumber: string;
  description: string;
  outcome: string;
  icon: "plane" | "shield";
  /** Served by Vite from public/samples/. */
  file: string;
}
export type Stage = "workorder" | "history" | "manuals" | "outlook";
export type StageStatus = "pending" | "running" | "complete" | "unavailable";
export interface Progress {
  stage: Stage;
  status: StageStatus;
}
export interface Quantiles {
  p50: number | null;
  p90: number | null;
  p95: number | null;
}
export interface RecommendationOutlook {
  status: "recommendation";
  componentKey: string;
  /** "recommend_inspection_or_part_planning" | "monitor" */
  action: string;
  /** "similar_workorders" | "component_history" | "fleet_replacement_interval" */
  basis: string;
  tac: Quantiles;
  lead: Quantiles;
  referenceTac: number | null;
  confidence: {
    level: string;
    similarity: number | null;
    sampleSize: number;
    cv: number | null;
  };
  evidence: { woId: string; sim: number | null }[];
  cyclesPerDay: number;
  /** YYYY-MM-DD of the analysis cutoff. */
  asOf: string;
  limitations: string[];
}
export type Outlook =
  | RecommendationOutlook
  | {
      status: "interval";
      componentKey: string | null;
      p50: number;
      p90: number;
      n: number;
      aircraft: number | null;
      asOf: string;
      limitations: string[];
    }
  | {
      status: "unavailable";
      reason: string;
      missing: string[];
      limitations: string[];
    };
export interface AnalysisRequest {
  document: WorkOrderDocument;
  workOrder: WorkOrder;
  component: Component;
  cyclesPerDay: number;
}
export interface AnalysisClient {
  listSamples(signal: AbortSignal): Promise<Sample[]>;
  loadSample(id: string, signal: AbortSignal): Promise<WorkOrderDocument>;
  readUpload(file: File, signal: AbortSignal): Promise<WorkOrderDocument>;
  analyze(
    request: AnalysisRequest,
    signal: AbortSignal,
    onProgress: (event: Progress) => void,
  ): Promise<Outlook>;
}
