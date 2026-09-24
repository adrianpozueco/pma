import type { AnalysisClient, Sample, Stage } from "../domain";
import { API_BASE, analyzeXml, buildAnalyzeUrl, mapAnalysis } from "./api";
import { parseXmlPreview } from "./xml";

const samples: Sample[] = [
  {
    id: "landing-light-rh",
    title: "RH landing light power supply",
    partNumber: "72605363-1",
    description:
      "Open work order on 9H-REG00427. Plan the next replacement from similar work orders.",
    outcome: "Live PMA analysis",
    icon: "plane",
    file: "samples/landing_light_rh.xml",
  },
  {
    id: "aft-smoke-detector",
    title: "AFT cargo smoke detector",
    partNumber: "473597-5",
    description:
      "AFT cargo fire loop A reported inoperative on SP-REG00374.",
    outcome: "Live PMA analysis",
    icon: "shield",
    file: "samples/aft_smoke_detector.xml",
  },
];

function abortIfNeeded(signal: AbortSignal) {
  signal.throwIfAborted();
}
/** Live adapter: samples and uploads both post their XML to /workorders/analyze. */
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
    const response = await fetch(`${import.meta.env.BASE_URL}${sample.file}`, {
      signal,
    });
    if (!response.ok)
      throw new Error("The example work order could not be loaded.");
    const document = parseXmlPreview(
      await response.text(),
      sample.file.split("/").pop() ?? sample.file,
    );
    return { ...document, source: "sample", sampleId: sample.id };
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
    const stages: Stage[] = ["workorder", "history", "manuals", "outlook"];
    // One backend call covers every stage; show them all running meanwhile.
    for (const stage of stages) onProgress({ stage, status: "running" });
    const url = buildAnalyzeUrl(
      API_BASE,
      request.workOrder,
      request.document.orders.length,
      new Date(),
    );
    const outlook = mapAnalysis(
      await analyzeXml(request.document.xml, url, signal),
      request.cyclesPerDay,
    );
    abortIfNeeded(signal);
    const found = outlook.status !== "unavailable";
    for (const stage of stages)
      onProgress({
        stage,
        status: found || stage === "workorder" ? "complete" : "unavailable",
      });
    return outlook;
  },
};
