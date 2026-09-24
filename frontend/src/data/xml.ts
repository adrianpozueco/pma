import type { Component, WorkOrderDocument } from "../domain";

const descriptions: Record<string, string> = {
  "2085M31G03": "Fuel-injection nozzle",
  "62197-301-001": "Water boiler",
  "8201-11-0000-01": "Convection oven",
};

const descendants = (root: Element | Document, name: string) =>
  Array.from(root.getElementsByTagNameNS("*", name));
const first = (root: Element | Document, name: string) =>
  descendants(root, name)[0];
const value = (root: Element | Document, name: string) =>
  first(root, name)?.textContent?.trim() || null;

/** Browser preview only. Server-side AMOS validation remains authoritative. */
export function parseXmlPreview(
  xml: string,
  filename: string,
): WorkOrderDocument {
  const dom = new DOMParser().parseFromString(xml, "application/xml");
  if (descendants(dom, "parsererror").length)
    throw new Error(
      "This XML could not be read. Check that the file is a complete XML export.",
    );
  // Refuse doctypes on the parsed tree, never on the raw text: a CDATA section or a
  // prolog comment may legitimately mention "<!DOCTYPE". XML forbids <!ENTITY outside
  // a DTD, so a null doctype rules out both declarations.
  if (dom.doctype !== null)
    throw new Error(
      "XML document types and entity declarations are not supported. Choose an AMOS XML export.",
    );
  const nodes = descendants(dom, "workorder");
  if (!nodes.length)
    throw new Error(
      "No work orders were found. Choose an AMOS work-order XML file.",
    );
  const orders = nodes.map((node, index) => {
    const header = first(node, "workorderHeader") || node;
    const components: Component[] = [];
    for (const tag of ["component", "partOff", "partOn", "partRequest"]) {
      for (const item of descendants(node, tag)) {
        const partNumber = value(item, "partNumber");
        if (!partNumber) continue;
        const role =
          tag === "partOff"
            ? "removed"
            : tag === "partOn"
              ? "installed"
              : "recorded";
        const serial = value(item, "serialNumber");
        if (
          components.some(
            (c) =>
              c.partNumber === partNumber &&
              c.serial === serial &&
              c.role === role,
          )
        )
          continue;
        components.push({
          id: `${index}-${components.length}`,
          partNumber,
          description:
            value(item, "description") ||
            descriptions[partNumber] ||
            "Component",
          serial,
          role,
          position:
            value(item, "position") ||
            (tag === "partOff" || tag === "partOn"
              ? value(item.parentElement!, "position")
              : null),
        });
      }
    }
    const steps = first(node, "workSteps");
    const issue = first(header, "issueData");
    return {
      id: value(node, "workorderNumber") || `Unnamed work order ${index + 1}`,
      aircraft: value(header, "aircraftFullRegistration"),
      aircraftType: value(header, "aircraftType"),
      status:
        value(header, "workorderState") === "C"
          ? "Closed · historical review"
          : value(header, "workorderState") === "O"
            ? "Open work order"
            : "Status not recorded",
      issuedAt: issue ? value(issue, "issueDateTime") : null,
      issueCycles: issue ? value(issue, "issueTac") : null,
      symptoms: steps
        ? descendants(steps, "workStep")
            .map((step) => value(step, "description"))
            .filter(Boolean)
            .join("\n") || "No symptom description recorded."
        : "No symptom description recorded.",
      components,
    };
  });
  return { filename, source: "upload", xml, orders };
}
