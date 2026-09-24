import { useCallback, useEffect, useRef, useState } from "react";
import type { KeyboardEvent } from "react";
import type {
  AnalysisRequest,
  Component,
  Outlook,
  Progress,
  RecommendationOutlook,
  Sample,
  Stage,
  StageStatus,
  WorkOrderDocument,
} from "./domain";
import {
  actionText,
  basisText,
  formatConfidence,
  formatHeadline,
  formatStatus,
} from "./data/api";
import { analysisClient } from "./data/client";
import ErrorBoundary from "./ErrorBoundary";
import { Icon } from "./Icons";

const stages: { id: Stage; title: string; detail: string; icon: string }[] = [
  {
    id: "workorder",
    title: "Read the work order",
    detail: "Identify the component and reported symptoms",
    icon: "file",
  },
  {
    id: "history",
    title: "Check maintenance history",
    detail: "Find relevant events and comparable records",
    icon: "search",
  },
  {
    id: "manuals",
    title: "Consult component manuals",
    detail: "Connect the part to technical references",
    icon: "book",
  },
  {
    id: "outlook",
    title: "Prepare the replacement outlook",
    detail: "Bring the evidence and available estimates together",
    icon: "layers",
  },
];
const initialProgress = (): Record<Stage, StageStatus> => ({
  workorder: "pending",
  history: "pending",
  manuals: "pending",
  outlook: "pending",
});
// The utilisation assumption is bounded in one place: the input attributes, the
// analyse() guard and the submit button all read the same numbers.
const MIN_CPD = 0.1;
const MAX_CPD = 24;
const STEP_CPD = 0.1;
const DEFAULT_CYCLES_PER_DAY = "6";
const cyclesValid = (value: string) => {
  const n = Number(value);
  return (
    value.trim() !== "" && Number.isFinite(n) && n >= MIN_CPD && n <= MAX_CPD
  );
};
const tabIds = ["upload", "storage"] as const;
type TabId = (typeof tabIds)[number];
const display = (value: string | null) => value || "Not recorded";
const roleLabel = (role: Component["role"]) =>
  role === "recorded"
    ? "Recorded component"
    : role === "installed"
      ? "Installed component"
      : "Removed component";
/** Aircraft counters arrive as free text; show what was recorded rather than NaN. */
function formatCycles(value: string | null) {
  if (!value || !value.trim()) return "Not recorded";
  const n = Number(value);
  return Number.isFinite(n) ? n.toLocaleString("en-GB") : value;
}
const fmtDate = (date: string) =>
  new Date(`${date}T00:00:00Z`).toLocaleDateString("en-GB", {
    day: "numeric",
    month: "short",
    year: "numeric",
    timeZone: "UTC",
  });
const fmtCycles = (n: number) => n.toLocaleString("en-GB");
function futureDate(asOf: string, cycles: number, perDay: number) {
  const date = new Date(`${asOf}T00:00:00Z`);
  date.setUTCDate(date.getUTCDate() + Math.ceil(cycles / perDay));
  // An out-of-range assumption produces an invalid Date; toISOString() would throw.
  return Number.isFinite(date.getTime())
    ? date.toISOString().slice(0, 10)
    : null;
}
function formatWindow(start: string | null, end: string | null) {
  if (!start || !end) return "Unavailable";
  return new Intl.DateTimeFormat("en-GB", {
    day: "numeric",
    month: "short",
    year: "numeric",
    timeZone: "UTC",
  }).formatRange(new Date(`${start}T00:00:00Z`), new Date(`${end}T00:00:00Z`));
}

export default function App() {
  const [view, setView] = useState<"analyse" | "architecture">("analyse");
  const [step, setStep] = useState(0);
  const [tab, setTab] = useState<TabId>("upload");
  const [samples, setSamples] = useState<Sample[]>([]);
  const [document, setDocument] = useState<WorkOrderDocument | null>(null);
  const [orderIndex, setOrderIndex] = useState(0);
  const [componentId, setComponentId] = useState("");
  const [cyclesPerDay, setCyclesPerDay] = useState(DEFAULT_CYCLES_PER_DAY);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [dragging, setDragging] = useState(false);
  const [progress, setProgress] = useState(initialProgress);
  const [result, setResult] = useState<Outlook | null>(null);
  const [lastRequest, setLastRequest] = useState<AnalysisRequest | null>(null);
  const [announcement, setAnnouncement] = useState("");
  const active = useRef<AbortController | null>(null);
  const fileInput = useRef<HTMLInputElement>(null);
  const content = useRef<HTMLElement>(null);
  const firstRender = useRef<string | null>(null);
  const tabRefs = useRef<Record<TabId, HTMLButtonElement | null>>({
    upload: null,
    storage: null,
  });
  const order = document?.orders[orderIndex];
  const component = order?.components.find((item) => item.id === componentId);

  useEffect(() => {
    const controller = new AbortController();
    analysisClient
      .listSamples(controller.signal)
      .then(setSamples)
      .catch((e) => {
        if (e.name !== "AbortError") setError(e.message);
      });
    return () => {
      controller.abort();
      active.current?.abort();
    };
  }, []);
  const focusWorkspace = useCallback(() => {
    const el = content.current;
    if (!el) return;
    el.focus({ preventScroll: true });
    // Optional call: JSDOM (used by the UI tests) does not implement it.
    el.scrollIntoView?.({ block: "start", behavior: "smooth" });
  }, []);
  // Every journey move — forward, backward, or between views — hands focus back
  // to the workspace, so it never falls to <body>. Keyed on the destination
  // rather than a bare flag: StrictMode runs mount effects twice in development
  // and a flag would steal focus (and scroll past the hero) on first paint.
  useEffect(() => {
    const here = `${view}:${step}`;
    if (firstRender.current === here) return;
    const first = firstRender.current === null;
    firstRender.current = here;
    if (!first) focusWorkspace();
  }, [step, view, focusWorkspace]);

  function begin() {
    active.current?.abort();
    const controller = new AbortController();
    active.current = controller;
    return controller;
  }
  function reset() {
    active.current?.abort();
    setBusy(false);
    setStep(0);
    setDocument(null);
    setOrderIndex(0);
    setComponentId("");
    setResult(null);
    setError("");
    setProgress(initialProgress());
    setLastRequest(null);
    setAnnouncement("");
    setCyclesPerDay(DEFAULT_CYCLES_PER_DAY);
    setView("analyse");
  }
  /** One entry point for the tab buttons and the side-panel cross-link: a pending
   * load must not resolve into the other tab and repopulate a stale selection. */
  function switchTab(next: TabId) {
    if (next === tab) return;
    active.current?.abort();
    setBusy(false);
    setTab(next);
    setDocument(null);
    setOrderIndex(0);
    setComponentId("");
    setError("");
  }
  function onTabKeyDown(event: KeyboardEvent<HTMLDivElement>) {
    const index = tabIds.indexOf(tab);
    const next =
      event.key === "ArrowRight"
        ? (index + 1) % tabIds.length
        : event.key === "ArrowLeft"
          ? (index - 1 + tabIds.length) % tabIds.length
          : event.key === "Home"
            ? 0
            : event.key === "End"
              ? tabIds.length - 1
              : -1;
    if (next < 0) return;
    event.preventDefault();
    switchTab(tabIds[next]);
    // A re-render alone leaves focus on the old button.
    tabRefs.current[tabIds[next]]?.focus();
  }
  async function load(source: File | string) {
    const controller = begin();
    setBusy(true);
    setError("");
    try {
      const next =
        typeof source === "string"
          ? await analysisClient.loadSample(source, controller.signal)
          : await analysisClient.readUpload(source, controller.signal);
      if (controller.signal.aborted) return;
      setDocument(next);
      setOrderIndex(0);
      setComponentId(
        next.orders[0]?.components.length === 1
          ? next.orders[0].components[0].id
          : "",
      );
    } catch (e) {
      if (!controller.signal.aborted) {
        setDocument(null);
        setError(
          e instanceof Error
            ? e.message
            : "The file could not be opened. Please try again.",
        );
      }
    } finally {
      if (!controller.signal.aborted) setBusy(false);
    }
  }
  function selectOrder(index: number) {
    setOrderIndex(index);
    setComponentId(
      document?.orders[index].components.length === 1
        ? document.orders[index].components[0].id
        : "",
    );
    setError("");
  }
  async function analyse() {
    if (!document || !order || !component || !cyclesValid(cyclesPerDay)) return;
    const controller = begin();
    setBusy(true);
    setError("");
    setResult(null);
    setAnnouncement("");
    setProgress(initialProgress());
    setStep(2);
    const request = {
      document,
      workOrder: order,
      component,
      cyclesPerDay: Number(cyclesPerDay),
    };
    setLastRequest(request);
    try {
      const next = await analysisClient.analyze(
        request,
        controller.signal,
        (event: Progress) => {
          if (!controller.signal.aborted)
            setProgress((prev) => ({ ...prev, [event.stage]: event.status }));
        },
      );
      if (!controller.signal.aborted) {
        setResult(next);
        setStep(3);
        setAnnouncement(
          `Analysis complete — ${
            {
              recommendation: "recommendation",
              interval: "historical interval",
              unavailable: "estimate unavailable",
            }[next.status]
          } for ${component.description}`,
        );
      }
    } catch (e) {
      if (!controller.signal.aborted) {
        setError(
          e instanceof Error
            ? e.message
            : "Analysis could not finish. Please try again.",
        );
        setStep(1);
      }
    } finally {
      if (!controller.signal.aborted) setBusy(false);
    }
  }
  function cancel() {
    active.current?.abort();
    setBusy(false);
    setAnnouncement("");
    setStep(1);
  }

  return (
    <>
      <header className="header">
        <div className="header-inner">
          <button
            className="brand"
            onClick={reset}
            aria-label="Ryanair Maintenance Intelligence home"
          >
            <span className="harp" aria-hidden="true" />
            <span className="wordmark">RYANAIR</span>
            <span className="brand-rule" />
            <span className="brand-product">
              MAINTENANCE
              <br />
              <strong>INTELLIGENCE</strong>
            </span>
          </button>
          <nav aria-label="Main navigation">
            <button
              className={view === "analyse" ? "nav-link active" : "nav-link"}
              aria-current={view === "analyse" ? "page" : undefined}
              onClick={() => {
                setError("");
                setView("analyse");
              }}
            >
              Analyse work order
            </button>
            <button
              className={
                view === "architecture" ? "nav-link active" : "nav-link"
              }
              aria-current={view === "architecture" ? "page" : undefined}
              onClick={() => {
                setError("");
                setView("architecture");
              }}
            >
              How it works <Icon name="layers" size={16} />
            </button>
          </nav>
          <span className="capstone-badge">
            GOOGLE ASL <span>CAPSTONE</span>
          </span>
        </div>
      </header>
      <div className="preview-bar">
        <div className="container">
          <span>
            <span className="status-dot" />
            Frontend preview
          </span>
          <span>Example work orders · Analysed by the local PMA backend</span>
        </div>
      </div>
      {view === "architecture" ? (
        <Architecture onStart={() => setView("analyse")} />
      ) : (
        <>
          <section className="hero">
            <div className="container hero-inner">
              <div className="hero-copy">
                <p className="eyebrow">AHEAD OF THE NEXT REPLACEMENT</p>
                <h1>
                  Every work order.
                  <br />
                  <span>A clearer outlook.</span>
                </h1>
                <p>
                  Turn maintenance records into component insight.
                  <br className="desktop-only" /> Explore what’s next, backed by
                  the evidence.
                </p>
              </div>
            </div>
          </section>
          <div className="container journey-wrap">
            <ol className="journey" aria-label="Analysis progress">
              {[
                "Select work order",
                "Review component",
                "Analyse",
                "Replacement outlook",
              ].map((label, index) => (
                <li
                  key={label}
                  className={
                    index === step ? "current" : index < step ? "done" : ""
                  }
                  aria-current={index === step ? "step" : undefined}
                >
                  {/* One authored phrase carries the number, the label and the
                      state, so the tick never swallows the step number. */}
                  <span className="visually-hidden">
                    {`Step 0${index + 1}, ${label}, ${
                      index < step
                        ? "completed"
                        : index === step
                          ? "current step"
                          : "not started"
                    }`}
                  </span>
                  <span className="step-number" aria-hidden="true">
                    {index < step ? (
                      <Icon name="check" size={17} />
                    ) : (
                      `0${index + 1}`
                    )}
                  </span>
                  <span aria-hidden="true">{label}</span>
                  {index < 3 && <Icon name="arrow" size={17} />}
                </li>
              ))}
            </ol>
          </div>
          <main
            className="container workspace"
            ref={content}
            tabIndex={-1}
            aria-labelledby="workspace-heading"
          >
            <div className="section-top">
              <div>
                <p className="eyebrow muted">
                  {
                    [
                      "YOUR STARTING POINT",
                      "CHECK THE DETAILS",
                      "CONNECTING THE EVIDENCE",
                      "THE COMPONENT OUTLOOK",
                    ][step]
                  }
                </p>
                <h2 id="workspace-heading">
                  {
                    [
                      "Let’s start with a work order",
                      "The right component. The right context.",
                      "From work order to insight",
                      "A clearer view of what’s next",
                    ][step]
                  }
                </h2>
              </div>
              {step > 0 && (
                <button className="text-button" onClick={reset}>
                  <Icon name="refresh" size={16} />
                  Start again
                </button>
              )}
            </div>
            {error && (
              <div className="error-banner" role="alert">
                <Icon name="info" />
                <span>{error}</span>
                <button
                  className="icon-button"
                  aria-label="Dismiss error"
                  onClick={() => {
                    setError("");
                    // Clearing the banner unmounts this button; move focus first.
                    focusWorkspace();
                  }}
                >
                  <Icon name="close" size={17} />
                </button>
              </div>
            )}
            {/* Mounted for the whole journey so the outcome text arrives into a
                region assistive technology is already observing. */}
            <p className="visually-hidden" role="status">
              {announcement}
            </p>
            <div className={step === 3 ? "results-layout" : "workspace-grid"}>
              <section className="main-panel">
                {step === 0 && (
                  <>
                    <div
                      className="tabs"
                      role="tablist"
                      aria-label="Work order source"
                      onKeyDown={onTabKeyDown}
                    >
                      <button
                        role="tab"
                        aria-selected={tab === "upload"}
                        aria-controls="source-panel"
                        id="upload-tab"
                        tabIndex={tab === "upload" ? 0 : -1}
                        ref={(el) => {
                          tabRefs.current.upload = el;
                        }}
                        className={tab === "upload" ? "selected" : ""}
                        onClick={() => switchTab("upload")}
                      >
                        <Icon name="upload" />
                        Upload XML
                      </button>
                      <button
                        role="tab"
                        aria-selected={tab === "storage"}
                        aria-controls="source-panel"
                        id="storage-tab"
                        tabIndex={tab === "storage" ? 0 : -1}
                        ref={(el) => {
                          tabRefs.current.storage = el;
                        }}
                        className={tab === "storage" ? "selected" : ""}
                        onClick={() => switchTab("storage")}
                      >
                        <Icon name="cloud" />
                        Cloud Storage
                      </button>
                    </div>
                    <div
                      className="panel-body"
                      id="source-panel"
                      role="tabpanel"
                      aria-labelledby={`${tab}-tab`}
                    >
                      {tab === "upload" ? (
                        <>
                          <div
                            className={`dropzone ${dragging ? "dragging" : ""}`}
                            onDragOver={(event) => {
                              event.preventDefault();
                              setDragging(true);
                            }}
                            onDragLeave={() => setDragging(false)}
                            onDrop={(event) => {
                              event.preventDefault();
                              setDragging(false);
                              if (event.dataTransfer.files.length > 1)
                                setError(
                                  "Choose one XML file at a time. A file can contain several work orders.",
                                );
                              else if (event.dataTransfer.files[0])
                                void load(event.dataTransfer.files[0]);
                            }}
                          >
                            <div
                              className="upload-illustration"
                              aria-hidden="true"
                            >
                              <div className="file-sheet">
                                <span>&lt;/&gt;</span>
                                <i />
                                <i />
                                <small>XML</small>
                              </div>
                              <span className="upload-bubble">
                                <Icon name="upload" size={18} />
                              </span>
                            </div>
                            <h3>Drop your work order here</h3>
                            <p>or choose an XML export from your computer</p>
                            <button
                              className="button secondary"
                              disabled={busy}
                              onClick={() => fileInput.current?.click()}
                            >
                              <Icon name="upload" size={17} />
                              Choose XML file
                            </button>
                            <span className="file-help">
                              AMOS XML format · Up to 25 MiB
                            </span>
                            <input
                              ref={fileInput}
                              type="file"
                              accept=".xml,application/xml,text/xml"
                              className="visually-hidden"
                              tabIndex={-1}
                              aria-label="Upload work-order XML"
                              onChange={(event) => {
                                if (event.target.files?.[0])
                                  void load(event.target.files[0]);
                                event.target.value = "";
                              }}
                            />
                          </div>
                          <p className="privacy-note">
                            <Icon name="shield" size={16} />
                            Your XML is sent only to the local analysis backend.
                          </p>
                        </>
                      ) : (
                        <>
                          <div className="library-heading">
                            <div>
                              <h3>Choose an example work order</h3>
                              <p>
                                Two real AMOS work orders, analysed live.
                              </p>
                            </div>
                            <span className="pill neutral">
                              EXAMPLE XML
                            </span>
                          </div>
                          <div className="sample-list">
                            {samples.map((sample) => (
                              <button
                                disabled={busy}
                                aria-pressed={document?.sampleId === sample.id}
                                className={`sample-card ${document?.sampleId === sample.id ? "chosen" : ""}`}
                                key={sample.id}
                                onClick={() => void load(sample.id)}
                              >
                                <span className="sample-icon">
                                  <Icon name={sample.icon} size={27} />
                                </span>
                                <span className="sample-copy">
                                  <strong>{sample.title}</strong>
                                  <span className="part-number">
                                    PN {sample.partNumber}
                                  </span>
                                  <span>{sample.description}</span>
                                  <small>{sample.outcome}</small>
                                </span>
                                <span className="sample-radio">
                                  {document?.sampleId === sample.id && <span />}
                                </span>
                              </button>
                            ))}
                          </div>
                          <p className="privacy-note">
                            <Icon name="info" size={16} />
                            Examples ship with the app; no Cloud Storage bucket
                            is connected.
                          </p>
                        </>
                      )}
                      {document && (
                        <div className="selected-file" role="status">
                          <span className="selected-file-icon">
                            <Icon name="file" />
                          </span>
                          <div>
                            <strong>{document.filename}</strong>
                            <span>
                              {document.orders.length} work order
                              {document.orders.length > 1 ? "s" : ""} ·{" "}
                              {document.source === "upload"
                                ? "Uploaded XML"
                                : "Example work order"}
                            </span>
                          </div>
                          <Icon name="check" size={20} />
                        </div>
                      )}
                      <div className="panel-actions">
                        <span>
                          {document
                            ? "Ready to review the extracted details"
                            : "Select a file to get started"}
                        </span>
                        <button
                          className="button primary"
                          disabled={!document || busy}
                          onClick={() => setStep(1)}
                        >
                          {busy ? "Reading XML…" : "Review work order"}
                          <Icon name="arrow" size={18} />
                        </button>
                      </div>
                    </div>
                  </>
                )}
                {step === 1 && order && document && (
                  <div className="panel-body review-panel">
                    <div className="review-header">
                      <span className="pill blue">
                        {document.source === "sample"
                          ? "EXAMPLE WORK ORDER"
                          : "UPLOADED XML"}
                      </span>
                      <span className="subtle-text">{order.status}</span>
                    </div>
                    {document.orders.length > 1 ? (
                      <label className="field-label">
                        Select work order
                        <select
                          value={orderIndex}
                          onChange={(e) => selectOrder(Number(e.target.value))}
                        >
                          {document.orders.map((item, index) => (
                            <option value={index} key={`${item.id}-${index}`}>
                              {item.id} · {display(item.aircraft)}
                            </option>
                          ))}
                        </select>
                      </label>
                    ) : (
                      <h3 className="workorder-title">Work order {order.id}</h3>
                    )}
                    <div className="facts">
                      <div>
                        <span>Aircraft</span>
                        <strong>{display(order.aircraft)}</strong>
                      </div>
                      <div>
                        <span>Aircraft type</span>
                        <strong>{display(order.aircraftType)}</strong>
                      </div>
                      <div>
                        <span>Aircraft cycles at issue</span>
                        <strong>{formatCycles(order.issueCycles)}</strong>
                      </div>
                    </div>
                    <div className="symptom">
                      <span className="field-caption">REPORTED SYMPTOM</span>
                      <p>{order.symptoms}</p>
                    </div>
                    <h3 className="small-heading" id="component-question">
                      Which component would you like to analyse?
                    </h3>
                    {!order.components.length && (
                      <div className="notice">
                        <Icon name="info" />
                        <p>
                          No component part numbers were found in this work
                          order. Choose another file or work order.
                        </p>
                      </div>
                    )}
                    <div
                      className="component-list"
                      role="radiogroup"
                      aria-labelledby="component-question"
                    >
                      {order.components.map((item) => (
                        <label
                          key={item.id}
                          className={`component-option ${item.id === componentId ? "chosen" : ""}`}
                        >
                          <input
                            type="radio"
                            name="component"
                            value={item.id}
                            checked={item.id === componentId}
                            onChange={() => setComponentId(item.id)}
                          />
                          <span>
                            <strong>{item.description}</strong>
                            <span>
                              PN {item.partNumber} · Serial{" "}
                              {display(item.serial)}
                            </span>
                            <small>
                              {display(item.position)} · {roleLabel(item.role)}
                            </small>
                          </span>
                        </label>
                      ))}
                    </div>
                    {order.status.startsWith("Closed") && (
                      <div className="notice">
                        <Icon name="clock" />
                        <p>
                          This is a closed work order. Its details describe a
                          historical event, not the present condition of an
                          installed component.
                        </p>
                      </div>
                    )}
                    <div className="utilisation">
                      <div>
                        <label htmlFor="cycles">
                          Expected flight cycles per day
                        </label>
                        <p>
                          One cycle is one take-off and landing. Used only for
                          an approximate calendar window.
                        </p>
                      </div>
                      <input
                        id="cycles"
                        type="number"
                        min={MIN_CPD}
                        max={MAX_CPD}
                        step={STEP_CPD}
                        value={cyclesPerDay}
                        onChange={(e) => setCyclesPerDay(e.target.value)}
                      />
                    </div>
                    <div className="panel-actions">
                      <button
                        className="text-button"
                        onClick={() => setStep(0)}
                      >
                        <Icon name="back" size={16} />
                        Change source
                      </button>
                      <button
                        className="button primary"
                        disabled={
                          !component || busy || !cyclesValid(cyclesPerDay)
                        }
                        onClick={() => void analyse()}
                      >
                        Analyse component
                        <Icon name="arrow" size={18} />
                      </button>
                    </div>
                  </div>
                )}
                {step === 2 && (
                  <div className="panel-body analysis-panel">
                    <div className="analysis-heading">
                      <span className="pulse-orbit">
                        <Icon name="engine" size={28} />
                      </span>
                      <h3>Building the component picture</h3>
                      <p>
                        Sending the work order to the PMA backend and matching
                        it against maintenance history.
                      </p>
                    </div>
                    <div className="analysis-stages" aria-live="polite">
                      {stages.map((item) => (
                        <div
                          key={item.id}
                          className={`analysis-stage ${progress[item.id]}`}
                        >
                          <span className="stage-icon">
                            {progress[item.id] === "running" ? (
                              <span className="spinner" />
                            ) : (
                              <Icon
                                name={
                                  progress[item.id] === "complete"
                                    ? "check"
                                    : item.icon
                                }
                                size={20}
                              />
                            )}
                          </span>
                          <div>
                            <strong>{item.title}</strong>
                            <span>{item.detail}</span>
                          </div>
                          <small>
                            {/* The live region repeats this text; without the
                                stage name it announces a bare status word. */}
                            <span className="visually-hidden">
                              {item.title}:{" "}
                            </span>
                            {progress[item.id] === "running"
                              ? "In progress"
                              : progress[item.id] === "complete"
                                ? "Complete"
                                : progress[item.id] === "unavailable"
                                  ? "Not connected"
                                  : "Waiting"}
                          </small>
                        </div>
                      ))}
                    </div>
                    <div className="analysis-bottom">
                      <span>Preview activity · No live agent calls</span>
                      <button className="text-button" onClick={cancel}>
                        Cancel analysis
                      </button>
                    </div>
                  </div>
                )}
                {step === 3 && result && lastRequest && (
                  <ErrorBoundary onReset={reset}>
                    <Results
                      result={result}
                      request={lastRequest}
                      onReset={reset}
                    />
                  </ErrorBoundary>
                )}
              </section>
              {step !== 3 && (
                <aside className="sidebar">
                  {step === 0 ? (
                    <>
                      <div className="side-card introduction">
                        <span className="mini-kicker">
                          MAINTENANCE, WITH FORESIGHT
                        </span>
                        <h3>
                          Small signals.{" "}
                          <br />
                          A bigger picture.
                        </h3>
                        <p>
                          Follow a component from its reported issue to the
                          evidence behind its next replacement.
                        </p>
                        <div className="value-item">
                          <Icon name="file" />
                          <div>
                            <strong>Start with what you know</strong>
                            <span>
                              An XML work order is your starting point.
                            </span>
                          </div>
                        </div>
                        <div className="value-item">
                          <Icon name="layers" />
                          <div>
                            <strong>Bring the evidence together</strong>
                            <span>History, manuals and component context.</span>
                          </div>
                        </div>
                        <div className="value-item">
                          <Icon name="calendar" />
                          <div>
                            <strong>Understand what comes next</strong>
                            <span>
                              Cycles, timing and what’s still missing.
                            </span>
                          </div>
                        </div>
                      </div>
                      <button
                        className="try-sample"
                        onClick={() =>
                          switchTab(tab === "upload" ? "storage" : "upload")
                        }
                      >
                        <span>
                          <strong>
                            {tab === "upload"
                              ? "Just exploring?"
                              : "Have your own work order?"}
                          </strong>
                          <span>
                            {tab === "upload"
                              ? "Try an example work order"
                              : "Upload an AMOS XML export"}
                          </span>
                        </span>
                        <Icon name="arrow" />
                      </button>
                    </>
                  ) : (
                    <>
                      <div className="side-card context-card">
                        <span className="mini-kicker">YOUR WORK ORDER</span>
                        <div className="context-icon">
                          <Icon name="aircraft" size={25} />
                        </div>
                        <h3>{display(order?.aircraft || null)}</h3>
                        <p>{document?.filename}</p>
                        <dl>
                          <div>
                            <dt>Component</dt>
                            <dd>
                              {component?.description || "Select a component"}
                            </dd>
                          </div>
                          <div>
                            <dt>Part number</dt>
                            <dd>{component?.partNumber || "—"}</dd>
                          </div>
                          <div>
                            <dt>Source</dt>
                            <dd>
                              {document?.source === "sample"
                                ? "Example work order"
                                : "Uploaded XML"}
                            </dd>
                          </div>
                        </dl>
                      </div>
                      <div className="small-note">
                        <Icon name="info" />
                        <p>
                          Aircraft total cycles are not component age. Missing
                          history stays visible throughout the analysis.
                        </p>
                      </div>
                    </>
                  )}
                </aside>
              )}
            </div>
            <div className="technology-strip">
              <span>A GOOGLE CLOUD LEARNING PROJECT</span>
              <div>
                <Icon name="cloud" size={17} />
                <strong>Cloud Run</strong>
                <i />
                <strong>Agent Development Kit</strong>
                <i />
                <strong>BigQuery</strong>
                <i />
                <strong>Vertex AI Search</strong>
              </div>
            </div>
          </main>
        </>
      )}
      <footer className="footer container">
        <span>
          RYANAIR <span>Maintenance Intelligence</span>
        </span>
        <span>Google ASL capstone · Demonstration only</span>
      </footer>
    </>
  );
}

function Results({
  result,
  request,
  onReset,
}: {
  result: Outlook;
  request: AnalysisRequest;
  onReset: () => void;
}) {
  const closed = request.workOrder.status.startsWith("Closed");
  const pill = {
    recommendation: ["yellow", "RECOMMENDATION"],
    interval: ["blue", "HISTORICAL INTERVAL"],
    unavailable: ["neutral", "ESTIMATE UNAVAILABLE"],
  }[result.status];
  return (
    <div className="panel-body result-panel">
      <div className="result-identity">
        <div>
          {/* The status rides in the eyebrow: .result-identity p:last-child
              styles the part-number line, so no trailing <p> may be added. */}
          <p className="eyebrow muted">
            {display(request.workOrder.aircraft)} / {request.workOrder.id} ·{" "}
            {request.workOrder.status}
          </p>
          {/* The backend's matched component key (PN|position) names the part
              more precisely than the XML, which often has no description. */}
          <h3>
            {result.status !== "unavailable" && result.componentKey
              ? result.componentKey
              : request.component.description}
          </h3>
          <p>
            PN {request.component.partNumber} <span>·</span> Serial{" "}
            {display(request.component.serial)} <span>·</span>{" "}
            {roleLabel(request.component.role)}
          </p>
        </div>
        <span className={`pill ${pill[0]}`}>{pill[1]}</span>
      </div>
      <div
        className={`result-notice ${result.status === "recommendation" ? "demo" : ""}`}
      >
        <Icon name="info" />
        <p>
          {result.status === "recommendation"
            ? "Heuristic estimate, not a calibrated forecast."
            : result.status === "interval"
              ? "Only a historical replacement interval is available for this component; no replacement timing is estimated."
              : result.reason}
        </p>
      </div>
      {closed && (
        <div className="notice">
          <Icon name="clock" />
          <p>
            This is a closed work order. Its details describe a historical
            event, not the present condition of an installed component.
          </p>
        </div>
      )}
      {result.status === "recommendation" && (
        <RecommendationDetail result={result} />
      )}
      {result.status === "interval" && (
        <div className="recommendation">
          <Icon name="refresh" />
          <div>
            <h4 className="rec-title">
              Observed historical interval (not a forecast)
            </h4>
            <p className="rec-headline">
              {`p50 ${fmtCycles(result.p50)} cycles · p90 ${fmtCycles(result.p90)} cycles (n=${result.n}${result.aircraft === null ? "" : `, ${result.aircraft} aircraft`})`}
            </p>
          </div>
        </div>
      )}
      {result.status === "unavailable" && result.missing.length > 0 && (
        <div className="missing-card">
          <h4>What’s needed for an estimate</h4>
          {result.missing.map((item) => (
            <div key={item}>
              <Icon name="info" size={16} />
              <span>{item}</span>
            </div>
          ))}
        </div>
      )}
      {result.limitations.length > 0 && (
        <div className="missing-card">
          <h4>Limitations</h4>
          {result.limitations.map((item) => (
            <div key={item}>
              <Icon name="info" size={16} />
              <span>{item}</span>
            </div>
          ))}
        </div>
      )}
      <div className="panel-actions">
        <span>
          {request.document.source === "sample"
            ? "Example work order · Live PMA analysis"
            : "Uploaded XML · Live PMA analysis"}
        </span>
        <button className="button primary" onClick={onReset}>
          Analyse another work order
          <Icon name="arrow" size={18} />
        </button>
      </div>
    </div>
  );
}

function RecommendationDetail({ result }: { result: RecommendationOutlook }) {
  const { lead, cyclesPerDay: cpd } = result;
  const status = formatStatus(result);
  const spread = [
    lead.p90 !== null && `p90 ${fmtCycles(lead.p90)}`,
    lead.p95 !== null && `p95 ${fmtCycles(lead.p95)}`,
  ].filter(Boolean);
  const dateAt = (cycles: number | null) =>
    cycles === null ? null : futureDate(result.asOf, cycles, cpd);
  return (
    <>
      <div className="recommendation">
        <Icon name="shield" />
        <div>
          <h4 className="rec-title">
            {actionText(result.action)} — {result.componentKey}
          </h4>
          <p className="rec-headline">{formatHeadline(result)}</p>
          {status && <p className="rec-status">{status}</p>}
          <p className="rec-confidence">
            <span className="pill blue">
              {result.confidence.level.toUpperCase()} CONFIDENCE
            </span>
            <span>{formatConfidence(result)}</span>
          </p>
          <p className="rec-basis">
            Basis: {basisText(result.basis)}
            {result.evidence.map((item, i) => (
              <a
                className="rec-cite"
                href={`#rec-evidence-${i + 1}`}
                key={item.woId}
                title={`WO ${item.woId}`}
              >
                [{i + 1}]
              </a>
            ))}
          </p>
        </div>
      </div>
      <div className="result-metrics">
        <article className="metric-card cycles-card">
          <span>
            <Icon name="refresh" />
            Cycles before p50
          </span>
          <strong>
            {lead.p50 === null ? "Unavailable" : fmtCycles(lead.p50)}
          </strong>
          <p>{spread.length ? spread.join(" · ") : "flight cycles"}</p>
        </article>
        <article className="metric-card">
          <span>
            <Icon name="calendar" />
            Estimated calendar window
          </span>
          <strong className="date-metric">
            {formatWindow(dateAt(lead.p50), dateAt(lead.p90))}
          </strong>
          <p>
            Assuming {cpd} flight cycle{cpd === 1 ? "" : "s"} / day
          </p>
        </article>
      </div>
      {result.evidence.length > 0 && (
        <>
          <div className="evidence-heading rec-evidence-heading">
            <h4>Similar work orders</h4>
            <span>{result.evidence.length} matched</span>
          </div>
          <ul className="rec-evidence">
            {result.evidence.map((item, i) => (
              <li id={`rec-evidence-${i + 1}`} key={item.woId}>
                <strong>
                  <span className="rec-cite-number">[{i + 1}]</span> WO{" "}
                  {item.woId}
                </strong>
                {item.sim !== null && (
                  <span>similarity {item.sim.toFixed(2)}</span>
                )}
              </li>
            ))}
          </ul>
        </>
      )}
    </>
  );
}

function Architecture({ onStart }: { onStart: () => void }) {
  return (
    <main className="container architecture">
      <p className="eyebrow muted">BEHIND THE EXPERIENCE</p>
      <h1>
        One work order.
        <br />
        <span>A connected cloud workflow.</span>
      </h1>
      <p className="architecture-intro">
        A Google ASL capstone exploring how agents can bring aircraft
        maintenance evidence together.
      </p>
      <div className="notice architecture-notice">
        <Icon name="info" />
        <p>
          Target architecture. Today the frontend calls a locally running PMA
          backend; the cloud deployment is still in progress.
        </p>
      </div>
      <h2 className="visually-hidden">The pipeline</h2>
      <section
        className="architecture-flow"
        aria-label="Proposed cloud architecture"
      >
        <div className="architecture-node">
          <span className="arch-icon">
            <Icon name="upload" size={26} />
          </span>
          <small>01 / INPUT</small>
          <h3>A work order arrives</h3>
          <p>
            Upload an AMOS XML file or select a prepared file from Cloud
            Storage.
          </p>
          <span className="pill blue">XML + CLOUD STORAGE</span>
        </div>
        <span className="connector">
          <Icon name="arrow" size={25} />
        </span>
        <div className="architecture-node">
          <span className="arch-icon">
            <Icon name="cloud" size={26} />
          </span>
          <small>02 / APPLICATION</small>
          <h3>A clear starting point</h3>
          <p>
            The frontend and server adapter on Cloud Run validate the input and
            start analysis.
          </p>
          <span className="pill blue">CLOUD RUN</span>
        </div>
        <span className="connector">
          <Icon name="arrow" size={25} />
        </span>
        <div className="architecture-node">
          <span className="arch-icon">
            <Icon name="layers" size={26} />
          </span>
          <small>03 / ORCHESTRATION</small>
          <h3>Agents gather evidence</h3>
          <p>
            ADK on Agent Runtime coordinates BigQuery history and Vertex AI
            Search manuals.
          </p>
          <span className="pill blue">ADK + AGENT RUNTIME</span>
        </div>
        <span className="connector">
          <Icon name="arrow" size={25} />
        </span>
        <div className="architecture-node">
          <span className="arch-icon">
            <Icon name="calendar" size={26} />
          </span>
          <small>04 / OUTLOOK</small>
          <h3>An explainable result</h3>
          <p>
            See supporting evidence and, when a validated model is available, a
            replacement estimate.
          </p>
          <span className="pill blue">STRUCTURED RESULTS</span>
        </div>
      </section>
      <section className="future-card">
        <div>
          <span className="pill yellow">THE NEXT CHAPTER</span>
          <h2>From manual selection to AMOS events.</h2>
          <p>
            A future AMOS integration could export work orders into a landing
            bucket. A Cloud Storage event would trigger the same analysis and
            deliver results to a maintenance inbox.
          </p>
          <p className="future-path">
            {/* Each arrow is glued to the step before it, so a line can only
                break after an arrow — never orphaning one at a line start. */}
            <span className="future-step">
              AMOS export
              <span className="future-arrow" aria-hidden="true">
                →
              </span>
            </span>{" "}
            <span className="future-step">
              Cloud Storage
              <span className="future-arrow" aria-hidden="true">
                →
              </span>
            </span>{" "}
            <span className="future-step">
              Event trigger
              <span className="future-arrow" aria-hidden="true">
                →
              </span>
            </span>{" "}
            <span className="future-step">ADK analysis</span>
          </p>
          <small>
            Future integration · Not connected in this capstone preview
          </small>
        </div>
        <span className="future-icon">
          <Icon name="plane" size={65} />
        </span>
      </section>
      <div className="architecture-bottom">
        <div>
          {/* Kept as an h3 element — six element-qualified rules across four
              breakpoints style it — but exposed at level 2 so the outline
              does not nest this section under the future-card heading. */}
          <h3 role="heading" aria-level={2}>
            Frontend and backend can evolve independently.
          </h3>
          <p>
            A typed adapter maps the analysis API onto the screens, so the
            same UI works against a local backend or a deployed service.
          </p>
        </div>
        <button className="button primary" onClick={onStart}>
          Explore the demo
          <Icon name="arrow" size={18} />
        </button>
      </div>
    </main>
  );
}
