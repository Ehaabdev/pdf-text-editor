import { useEffect, useLayoutEffect, useRef, useState } from "react";
import {
  ApiError,
  deleteLines,
  downloadUrl,
  goToVersion,
  redoEdit,
  undoEdit,
  type DocInfo,
  type EditOutcome,
  type PickedLine,
  type TextLine,
} from "../api";
import AddTextPanel, { type Spot } from "./AddTextPanel";
import ChangesPanel from "./ChangesPanel";
import LinePanel, { type Change } from "./LinePanel";
import PageView from "./PageView";
import SelectionPanel from "./SelectionPanel";

// On wide screens an A4 page (595pt) shows about 860px wide at 100%.
const CSS_PX_PER_PT = 1.45;
const GUTTER = 16;
const ZOOMS = [1, 1.5, 2, 3, 4];

function Icon({ d, size = 20 }: { d: string; size?: number }) {
  return (
    <svg viewBox="0 0 20 20" width={size} height={size} aria-hidden="true">
      <path d={d} fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

const CLOSE = "M5 5l10 10M15 5L5 15";
const DOWNLOAD = "M10 3v9m0 0l-3.5-3.5M10 12l3.5-3.5M4 14.5V16h12v-1.5";
const PLUS = "M10 4v12M4 10h12";
const MINUS = "M4 10h12";
const BOXES = "M3 5h9M3 10h14M3 15h11";
const UNDO = "M7.5 5.5L4 9l3.5 3.5M4.5 9H12a4 4 0 010 8h-2";
const REDO = "M12.5 5.5L16 9l-3.5 3.5M15.5 9H8a4 4 0 000 8h2";
const ADD_TEXT = "M3 4.5h8M7 4.5V15M12.5 12.5h5M15 10v5";
const CHANGES = "M7 5h10M7 10h10M7 15h10M3.5 5h.01M3.5 10h.01M3.5 15h.01";

function useWidth(ref: React.RefObject<HTMLElement | null>) {
  const [width, setWidth] = useState(0);
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    const ro = new ResizeObserver(() => setWidth(el.clientWidth));
    ro.observe(el);
    setWidth(el.clientWidth);
    return () => ro.disconnect();
  }, [ref]);
  return width;
}

type Notice = { text: string; tone: "done" | "warning" | "error" };

export default function Viewer({ doc, onClose, onChange }: { doc: DocInfo; onClose: () => void; onChange: (d: DocInfo) => void }) {
  const [notice, setNotice] = useState<Notice | null>(null);
  const [busy, setBusy] = useState(false);
  const [showChanges, setShowChanges] = useState(false);
  // Comparing with the original: which version the pages show, with the changes outlined.
  const [compare, setCompare] = useState<"original" | "now" | null>(null);

  // Confirmations fade after a while; warnings and errors stay until dismissed.
  useEffect(() => {
    if (notice?.tone !== "done") return;
    const t = setTimeout(() => setNotice(null), 5000);
    return () => clearTimeout(t);
  }, [notice]);

  // Nothing left to compare: back to editing.
  useEffect(() => {
    if (doc.version === 0) setCompare(null);
  }, [doc.version]);

  function report(what: string, outcome: EditOutcome) {
    setNotice(
      outcome.warnings.length
        ? { text: `${what} ${outcome.warnings.join(" ")}`, tone: "warning" }
        : { text: what, tone: "done" },
    );
  }

  function edited(outcome: EditOutcome, change: Change) {
    clearSelection();
    onChange(outcome.document);
    report(
      change === "deleted" ? "Line deleted. The space it used stays empty." : change === "added" ? "Line added." : "Line changed.",
      outcome,
    );
  }

  const [deleting, setDeleting] = useState(false);
  async function remove(lines: PickedLine[]) {
    if (!lines.length || deleting) return;
    setDeleting(true);
    try {
      const outcome = await deleteLines(doc, lines);
      clearSelection();
      onChange(outcome.document);
      report(lines.length === 1 ? "Line deleted. The space it used stays empty." : `${lines.length} lines deleted. The space they used stays empty.`, outcome);
    } catch (e) {
      setNotice({ text: e instanceof ApiError ? e.message : "Couldn't delete. Try again.", tone: "error" });
    } finally {
      setDeleting(false);
    }
  }

  /** Undo, redo, or jump to a version: the pages change, nothing else does. */
  async function move(action: () => Promise<DocInfo>, done: string, failed: string) {
    setBusy(true);
    try {
      onChange(await action());
      clearSelection();
      stopPlacing();
      setNotice({ text: done, tone: "done" });
    } catch (e) {
      setNotice({ text: e instanceof ApiError ? e.message : failed, tone: "error" });
    } finally {
      setBusy(false);
    }
  }
  const undo = () => move(() => undoEdit(doc), "Last change undone.", "Couldn't undo. Try again.");
  const redo = () => move(() => redoEdit(doc), "Change redone.", "Couldn't redo. Try again.");
  const goTo = (version: number) =>
    move(
      () => goToVersion(doc, version),
      version === 0 ? "Back to the original. Your changes can still be redone." : "Changes redone.",
      "Couldn't do that. Try again.",
    );

  const scroller = useRef<HTMLElement>(null);
  const available = useWidth(scroller);
  const [zoomStep, setZoomStep] = useState(0);
  const [showText, setShowText] = useState(false);
  // Selected lines, in the order they were picked. In "adding" mode (for touch screens)
  // every tap adds or removes a line; with a mouse, Shift or Ctrl does the same.
  const [picked, setPicked] = useState<PickedLine[]>([]);
  const [adding, setAdding] = useState(false);
  // Adding text: tap a spot on a page, then type.
  const [placing, setPlacing] = useState(false);
  const [spot, setSpot] = useState<Spot | null>(null);

  function stopPlacing() {
    setPlacing(false);
    setSpot(null);
  }
  const zoom = ZOOMS[zoomStep];

  function clearSelection() {
    setPicked([]);
    setAdding(false);
  }

  function startComparing(show: "original" | "now" = "original") {
    clearSelection();
    stopPlacing();
    setCompare(show);
  }

  /** Scroll so a spot on a page (points, as displayed) is in the middle of the view. */
  function scrollTo(page: number, x: number, y: number) {
    const el = scroller.current;
    const figure = document.getElementById(`page-${page}`);
    if (!el || !figure) return;
    const img = figure.querySelector(".page") as HTMLElement | null;
    const box = img ?? figure;
    const size = doc.pages[page];
    el.scrollTop = box.offsetTop + (y / size.height) * box.offsetHeight - el.clientHeight / 3;
    el.scrollLeft = box.offsetLeft + (x / size.width) * box.offsetWidth - el.clientWidth / 2;
  }

  function show(index: number) {
    const change = doc.history[index];
    const [page, x0, y0, x1, y1] = (change.marks ?? change.areas)[0];
    startComparing("now");
    requestAnimationFrame(() => scrollTo(page, (x0 + x1) / 2, (y0 + y1) / 2));
  }

  function select(page: number, line: TextLine | null, more: boolean) {
    if (!line) {
      if (!adding) setPicked([]);
      return;
    }
    setPicked((current) => {
      const has = current.some((p) => p.page === page && p.line.id === line.id);
      if (more || adding) return has ? current.filter((p) => !(p.page === page && p.line.id === line.id)) : [...current, { page, line }];
      return has && current.length === 1 ? [] : [{ page, line }];
    });
  }

  // Keep the middle of the view in place when zooming.
  const previousZoom = useRef(zoom);
  useLayoutEffect(() => {
    const el = scroller.current;
    const ratio = zoom / previousZoom.current;
    previousZoom.current = zoom;
    if (!el || ratio === 1) return;
    el.scrollTop = (el.scrollTop + el.clientHeight / 2) * ratio - el.clientHeight / 2;
    el.scrollLeft = (el.scrollLeft + el.clientWidth / 2) * ratio - el.clientWidth / 2;
  }, [zoom]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      // Not while typing, nor from inside the panel (Backspace on its buttons is a Mac's Delete).
      const typing = e.target instanceof HTMLElement && e.target.closest("input, textarea, [contenteditable], .line-panel");
      if (e.key === "Escape") {
        clearSelection();
        stopPlacing();
        setCompare(null);
      } else if ((e.key === "Delete" || e.key === "Backspace") && !typing && picked.length && !compare) {
        e.preventDefault();
        void remove(picked);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  });

  const pageWidth = (ptWidth: number) =>
    Math.max(200, Math.min(available - 2 * GUTTER, ptWidth * CSS_PX_PER_PT)) * zoom;

  // While comparing, the pages show a stored version, with every applied change outlined.
  const shownVersion = compare === "original" ? 0 : compare === "now" ? doc.version : undefined;
  const outlines = (page: number) =>
    compare
      ? doc.history.filter((c) => c.applied).flatMap((c) => (c.marks ?? c.areas).filter((a) => a[0] === page).map((a) => a.slice(1)))
      : [];
  const applied = doc.history.filter((c) => c.applied).length;

  return (
    <div
      className={`desk viewer${showText ? " viewer--show-text" : ""}${placing ? " viewer--placing" : ""}${compare ? " viewer--comparing" : ""}`}
    >
      <header className="topbar">
        <button className="btn btn-ghost" onClick={onClose} aria-label="Close this PDF and open another">
          <Icon d={CLOSE} />
          <span className="wide-only">Open another PDF</span>
        </button>
        <div className="doc-title">
          <span className="doc-name" title={doc.name}>
            {doc.name}
          </span>
          <span className="doc-pages">
            {doc.pageCount} {doc.pageCount === 1 ? "page" : "pages"}
          </span>
        </div>
        {doc.canUndo && (
          <button className="btn btn-ghost" onClick={() => void undo()} disabled={busy} aria-label="Undo the last change">
            <Icon d={UNDO} />
            <span className="wide-only">Undo</span>
          </button>
        )}
        {doc.canRedo && (
          <button className="btn btn-ghost" onClick={() => void redo()} disabled={busy} aria-label="Redo the change undone last">
            <Icon d={REDO} />
            <span className="wide-only">Redo</span>
          </button>
        )}
        {doc.history.length > 0 && (
          <button
            className="btn btn-ghost"
            aria-pressed={showChanges}
            onClick={() => setShowChanges((v) => !v)}
            aria-label={`Changes (${applied})`}
          >
            <Icon d={CHANGES} />
            <span className="wide-only">Changes</span>
            <span className="count">{applied}</span>
          </button>
        )}
        <a className="btn btn-primary" href={downloadUrl(doc)} download={doc.name}>
          <Icon d={DOWNLOAD} />
          <span>
            Download<span className="wide-only"> PDF</span>
          </span>
        </a>
      </header>

      {doc.isSigned && (
        <p className="banner" role="status">
          This PDF has a digital signature. Editing it will make that signature invalid.
        </p>
      )}

      {compare && (
        <div className="compare-bar" role="group" aria-label="Compare with the original">
          <div className="segmented">
            <button aria-pressed={compare === "original"} onClick={() => setCompare("original")}>
              Original
            </button>
            <button aria-pressed={compare === "now"} onClick={() => setCompare("now")}>
              With changes
            </button>
          </div>
          <button className="btn btn-ghost" onClick={() => setCompare(null)}>
            Done
          </button>
        </div>
      )}

      <main className="pages" ref={scroller}>
        <div className="pages-inner">
          {available > 0 &&
            doc.pages.map((p, i) => (
              <PageView
                key={`${doc.revision}-${i}`}
                doc={doc}
                index={i}
                width={pageWidth(p.width)}
                selectedIds={new Set(picked.filter((p) => p.page === i).map((p) => p.line.id))}
                onSelect={(line, more) => select(i, line, more)}
                placing={placing}
                spot={spot?.page === i ? spot : null}
                onPlace={(x, y) => setSpot({ page: i, x, y })}
                version={shownVersion}
                outlines={outlines(i)}
              />
            ))}
        </div>
      </main>

      <div className="viewer-bottom">
      <div className="view-controls" role="toolbar" aria-label="View">
        {!compare && (
          <button
            className="btn btn-control"
            aria-pressed={placing}
            onClick={() => {
              if (placing) stopPlacing();
              else {
                clearSelection();
                setPlacing(true);
              }
            }}
            title={placing ? "Stop adding text" : "Add text anywhere on the page"}
            aria-label={placing ? "Stop adding text" : "Add text anywhere on the page"}
          >
            <Icon d={ADD_TEXT} />
          </button>
        )}
        {!compare && (
          <button
            className="btn btn-control"
            aria-pressed={showText}
            onClick={() => setShowText((v) => !v)}
            title={showText ? "Hide text boxes" : "Show all text boxes"}
            aria-label={showText ? "Hide text boxes" : "Show all text boxes"}
          >
            <Icon d={BOXES} />
          </button>
        )}
        <div className="zoom-group">
          <button
            className="btn btn-control"
            onClick={() => setZoomStep((z) => Math.max(0, z - 1))}
            disabled={zoomStep === 0}
            aria-label="Zoom out"
          >
            <Icon d={MINUS} />
          </button>
          <span className="zoom-level" aria-live="polite">
            {Math.round(zoom * 100)}%
          </span>
          <button
            className="btn btn-control"
            onClick={() => setZoomStep((z) => Math.min(ZOOMS.length - 1, z + 1))}
            disabled={zoomStep === ZOOMS.length - 1}
            aria-label="Zoom in"
          >
            <Icon d={PLUS} />
          </button>
        </div>
      </div>

      {notice && (
        <div className={`viewer-notice viewer-notice--${notice.tone}`} role={notice.tone === "error" ? "alert" : "status"}>
          <p>{notice.text}</p>
          <button className="btn btn-ghost btn-icon" onClick={() => setNotice(null)} aria-label="Dismiss">
            <Icon d={CLOSE} size={16} />
          </button>
        </div>
      )}
      {showChanges ? (
        <ChangesPanel
          doc={doc}
          busy={busy}
          comparing={compare !== null}
          onClose={() => setShowChanges(false)}
          onShow={(k) => {
            setShowChanges(false);
            show(k);
          }}
          onCompare={() => (compare ? setCompare(null) : startComparing())}
          onGoTo={(v) => void goTo(v)}
        />
      ) : compare ? null : placing ? (
        spot ? (
          <AddTextPanel
            doc={doc}
            spot={spot}
            onClose={stopPlacing}
            onAdded={(outcome) => {
              stopPlacing();
              onChange(outcome.document);
              report("Text added.", outcome);
            }}
          />
        ) : (
          <section className="line-panel" aria-label="Add text">
            <div className="line-panel-head">
              <h2>Add text</h2>
              <button className="btn btn-ghost btn-icon" onClick={stopPlacing} aria-label="Stop adding text">
                <Icon d={CLOSE} size={18} />
              </button>
            </div>
            <p className="edit-note">Tap the page where the new text should start.</p>
          </section>
        )
      ) : picked.length === 1 && !adding ? (
        <LinePanel
          doc={doc}
          line={picked[0].line}
          page={picked[0].page}
          deleting={deleting}
          onClose={clearSelection}
          onEdited={edited}
          onDelete={() => void remove(picked)}
          onSelectMore={() => setAdding(true)}
        />
      ) : picked.length > 0 || adding ? (
        <SelectionPanel picked={picked} deleting={deleting} onDelete={() => void remove(picked)} onClose={clearSelection} />
      ) : null}
      </div>
    </div>
  );
}
