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
  orders: WorkOrder[];
}
export interface Sample {
  id: string;
  title: string;
  partNumber: string;
  description: string;
  outcome: string;
  icon: "engine" | "boiler" | "oven";
}
export type Stage = "workorder" | "history" | "manuals" | "outlook";
export type StageStatus = "pending" | "running" | "complete" | "unavailable";
export interface Progress {
  stage: Stage;
  status: StageStatus;
}
export interface Evidence {
  title: string;
  source: string;
  detail: string;
}
export type Outlook =
  | {
      status: "illustrative";
      rangeCycles: [number, number];
      cyclesPerDay: number;
      asOf: string;
      evidence: Evidence[];
      reason: string;
    }
  | {
      status: "unavailable";
      reason: string;
      missing: string[];
      evidence: Evidence[];
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
