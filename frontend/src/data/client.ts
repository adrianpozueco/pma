import type {
  AnalysisClient,
  Sample,
  Stage,
  WorkOrderDocument,
} from "../domain";
import { parseXmlPreview } from "./xml";

const samples: Sample[] = [
  {
    id: "nozzle",
    title: "Fuel-injection nozzle",
    partNumber: "2085M31G03",
    description:
      "An inspection flags nozzle wear. Explore the intended replacement outlook.",
    outcome: "Illustrative forecast",
    icon: "engine",
  },
  {
    id: "boiler",
    title: "Water boiler",
    partNumber: "62197-301-001",
    description:
      "An intermittent heating fault with an incomplete component history.",
    outcome: "Missing-data scenario",
    icon: "boiler",
  },
  {
    id: "oven",
    title: "Convection oven",
    partNumber: "8201-11-0000-01",
    description:
      "Uneven heating reported in the galley. Review a second forecast scenario.",
    outcome: "Illustrative forecast",
    icon: "oven",
  },
];

function abortIfNeeded(signal: AbortSignal) {
  signal.throwIfAborted();
}
function pause(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    abortIfNeeded(signal);
    const abort = () => {
      clearTimeout(timer);
      reject(new DOMException("Cancelled", "AbortError"));
    };
    const timer = setTimeout(() => {
      signal.removeEventListener("abort", abort);
      resolve();
    }, ms);
    signal.addEventListener("abort", abort, { once: true });
  });
}

function sampleDocument(sample: Sample): WorkOrderDocument {
  return {
    filename: `DEMO-${sample.id.toUpperCase()}.xml`,
    source: "sample",
    sampleId: sample.id,
    orders: [
      {
        id: `DEMO-${sample.id.toUpperCase()}-001`,
        aircraft: "DEMO-737",
        aircraftType: "Boeing 737-8200",
        status: "Open work order",
        issuedAt: "2026-09-23T09:00:00Z",
        issueCycles: "12480",
        symptoms:
          sample.id === "nozzle"
            ? "Synthetic sample: coating wear observed during a fuel nozzle inspection. Assess the component against comparable maintenance evidence."
            : sample.id === "boiler"
              ? "Synthetic sample: intermittent water heating. Component installation record is unavailable."
              : "Synthetic sample: uneven galley oven temperature reported during operation.",
        components: [
          {
            id: sample.id,
            partNumber: sample.partNumber,
            description: sample.title,
            serial: `DEMO-${sample.id.toUpperCase()}-01`,
            position: sample.id === "nozzle" ? "Engine 1" : "Forward galley",
            role: "recorded",
          },
        ],
      },
    ],
  };
}

/** Replace this adapter when the backend contract is ready. No live cloud calls. */
export const analysisClient: AnalysisClient = {
  async listSamples(signal) {
    abortIfNeeded(signal);
    return samples;
  },
  async loadSample(id, signal) {
    abortIfNeeded(signal);
    const sample = samples.find((item) => item.id === id);
    if (!sample)
      throw new Error(
        "This sample is no longer available. Choose another work order.",
      );
    return sampleDocument(sample);
  },
  async readUpload(file, signal) {
    if (!file.name.toLowerCase().endsWith(".xml"))
      throw new Error("Choose an XML file (.xml).");
    if (file.size > 25 * 1024 * 1024)
      throw new Error(
        "This file exceeds the 25 MiB limit. Choose a smaller XML export.",
      );
    if (!file.size)
      throw new Error("This file is empty. Choose a work-order XML export.");
    const text = await file.text();
    abortIfNeeded(signal);
    return parseXmlPreview(text, file.name);
  },
  async analyze(request, signal, onProgress) {
    const demo = request.document.source === "sample";
    const stages: Stage[] = ["workorder", "history", "manuals", "outlook"];
    for (const stage of stages) {
      abortIfNeeded(signal);
      onProgress({ stage, status: "running" });
      await pause(650, signal);
      onProgress({
        stage,
        status: demo || stage === "workorder" ? "complete" : "unavailable",
      });
    }
    if (!demo)
      return {
        status: "unavailable",
        reason:
          "Your XML has been read locally. Live maintenance evidence and prediction services are not connected in this frontend preview.",
        missing: [
          "Connected ADK analysis service",
          "Validated component forecast",
          "Verified current component history and utilisation",
        ],
        evidence: [
          {
            title: request.document.filename,
            source: "Uploaded XML · browser preview",
            detail: request.workOrder.symptoms,
          },
        ],
      };
    if (request.document.sampleId === "boiler")
      return {
        status: "unavailable",
        reason:
          "This demonstration shows how the app responds when component history is incomplete.",
        missing: [
          "Verified component installation date and counters",
          "Sufficient follow-up records",
          "Validated prediction model",
        ],
        evidence: [
          {
            title: "Intermittent heating reported",
            source: "Synthetic sample",
            detail:
              "A symptom alone does not establish remaining component life. The missing installation history is shown explicitly.",
          },
        ],
      };
    return {
      status: "illustrative",
      rangeCycles:
        request.document.sampleId === "oven" ? [300, 420] : [180, 260],
      cyclesPerDay: request.cyclesPerDay,
      asOf: "2026-09-23",
      reason:
        "A fictional result to demonstrate the planned experience. These numbers were not calculated by ADK or a prediction model.",
      evidence: [
        {
          title: "Reported component condition",
          source: "Synthetic sample",
          detail: request.workOrder.symptoms,
        },
        {
          title: "Comparable maintenance events",
          source: "Illustrative BigQuery evidence",
          detail:
            "In the connected version, dated maintenance records will appear here with traceable work-order references. No live records were queried.",
        },
        {
          title: "Component identification",
          source: "Illustrative manual lookup",
          detail: `The connected version will link relevant manual references for ${request.component.partNumber}. No manual was retrieved in this preview.`,
        },
      ],
    };
  },
};
