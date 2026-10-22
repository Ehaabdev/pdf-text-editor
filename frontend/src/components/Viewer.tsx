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
import CheckPanel from "./CheckPanel";
import HomePanel from "./HomePanel";
import { ADD_TEXT, BOXES, CHANGES, CLOSE, DOWNLOAD, Icon, MAGNIFIER, Mark, MINUS, PLUS, REDO, UNDO } from "./Icon";
import LinePanel, { type Change } from "./LinePanel";
import PageView, { type Flash } from "./PageView";
import SelectionPanel from "./SelectionPanel";
import ThemeSwitch from "./ThemeSwitch";

// On wide screens an A4 page (595pt) shows about 860px wide at 100%.
const CSS_PX_PER_PT = 1.45;
// Space beside the pages: matches .pages-inner's padding.
const gutter = (width: number) => (width < 640 ? 12 : 24);
const ZOOMS = [1, 1.5, 2, 3, 4];

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

type Notice = { text: string; tone: "done" | "warning" | "error"; detail?: string };
type HistoryEntry = DocInfo["history"][number];

// Every change the server accepts has passed its checks, among them that nothing outside
// the changed lines differs by a single pixel.
const CHECKED = "Checked: nothing else on the page changed.";

export default function Viewer({ doc, onClose, onChange }: { doc: DocInfo; onClose: () => void; onChange: (d: DocInfo) => void }) {
  const [notice, setNotice] = useState<Notice | null>(null);
  const [busy, setBusy] = useState(false);
  const [showChanges, setShowChanges] = useState(false);
  // Comparing with the original: which version the pages show, with the changes outlined.
  const [compare, setCompare] = useState<"original" | "now" | null>(null);
  // A close look at one change (doc.history index), opened after a change that used a
  // look-alike font, or from the list of changes.
  const [checking, setChecking] = useState<number | null>(null);

  // Confirmations fade after a while; warnings and errors stay until dismissed.
  useEffect(() => {
    if (notice?.tone !== "done") return;
    const t = setTimeout(() => setNotice(null), 5000);
    return () => clearTimeout(t);
  }, [notice]);

  // Where the last change was made: lit up on the page once its new image is showing.
  const [flash, setFlash] = useState<Flash | null>(null);
  useEffect(() => {
    if (!flash) return;
    const t = setTimeout(() => setFlash(null), 4000);
    return () => clearTimeout(t);
  }, [flash]);

  function lightUp(d: DocInfo, change: HistoryEntry | undefined) {
    const rects = change ? (change.marks ?? change.areas ?? []) : [];
    setFlash(rects.length ? { revision: d.revision, rects } : null);
  }

  // A panel is waiting for the server: the selected lines shimmer meanwhile.
  const [working, setWorking] = useState(false);

  // Nothing left to compare: back to editing.
  useEffect(() => {
    if (doc.version === 0) setCompare(null);
  }, [doc.version]);

  function report(what: string, outcome: EditOutcome) {
    // A look-alike font is explained in the check panel that opens: not twice.
    const subs = (outcome.lookalikes ?? []).map((l) => l.substitute);
    const warnings = outcome.warnings.filter((w) => !subs.some((s) => w.includes(s)));
    setNotice(
      warnings.length ? { text: `${what} ${warnings.join(" ")}`, tone: "warning" } : { text: what, tone: "done", detail: CHECKED },
    );
  }

  /** After a change that used a look-alike font: ask the user to check it. */
  function offerCheck(outcome: EditOutcome) {
    if (outcome.lookalikes?.length) setChecking(outcome.document.history.length - 1);
  }

  function edited(outcome: EditOutcome, change: Change) {
    clearSelection();
    onChange(outcome.document);
    lightUp(outcome.document, outcome.document.history.at(-1));
    offerCheck(outcome);
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
      lightUp(outcome.document, outcome.document.history.at(-1));
      report(lines.length === 1 ? "Line deleted. The space it used stays empty." : `${lines.length} lines deleted. The space they used stays empty.`, outcome);
    } catch (e) {
      setNotice({ text: e instanceof ApiError ? e.message : "Couldn't delete. Try again.", tone: "error" });
    } finally {
      setDeleting(false);
    }
  }

  /** Undo, redo, or jump to a version: the pages change, nothing else does. */
  async function move(action: () => Promise<DocInfo>, done: string, failed: string, light?: (d: DocInfo) => HistoryEntry | undefined) {
    setBusy(true);
    try {
      const d = await action();
      onChange(d);
      if (light) lightUp(d, light(d));
      setChecking(null);
      clearSelection();
      stopPlacing();
      setNotice({ text: done, tone: "done" });
    } catch (e) {
      setNotice({ text: e instanceof ApiError ? e.message : failed, tone: "error" });
    } finally {
      setBusy(false);
    }
  }
  // Undo lights up the change it took back; redo the one it put back.
  const undo = () => move(() => undoEdit(doc), "Last change undone.", "Couldn't undo. Try again.", (d) => d.history[d.version]);
  const redo = () => move(() => redoEdit(doc), "Change redone.", "Couldn't redo. Try again.", (d) => d.history[d.version - 1]);
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
  // A lens over the page that follows the mouse (wide screens).
  const [magnifier, setMagnifier] = useState(false);
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
    const where = (change.marks ?? change.areas ?? [])[0];
    if (!where) return;
    const [page, x0, y0, x1, y1] = where;
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
      const typing = e.target instanceof HTMLElement && e.target.closest("input, textarea, [contenteditable], .dock");
      if (e.key === "Escape") {
        clearSelection();
        stopPlacing();
        setCompare(null);
      } else if (e.key.toLowerCase() === "m" && !typing && !e.ctrlKey && !e.metaKey && !e.altKey) {
        setMagnifier((v) => !v);
      } else if ((e.key === "Delete" || e.key === "Backspace") && !typing && picked.length && !compare) {
        e.preventDefault();
        void remove(picked);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  });

  const pageWidth = (ptWidth: number) =>
    Math.max(200, Math.min(available - 2 * gutter(available), ptWidth * CSS_PX_PER_PT)) * zoom;

  // While comparing, the pages show a stored version, with every applied change outlined.
  const shownVersion = compare === "original" ? 0 : compare === "now" ? doc.version : undefined;
  const outlines = (page: number) =>
    compare
      ? doc.history.filter((c) => c.applied).flatMap((c) => (c.marks ?? c.areas ?? []).filter((a) => a[0] === page).map((a) => a.slice(1)))
      : [];
  const applied = doc.history.filter((c) => c.applied).length;

  const zoomControls = (
    <div className="zoom-group" role="group" aria-label="Zoom">
      <button
        className="btn btn-ghost btn-icon wide-only"
        aria-pressed={magnifier}
        onClick={() => setMagnifier((v) => !v)}
        aria-label="Magnifier"
        title="Magnifier (M)"
      >
        <Icon d={MAGNIFIER} />
      </button>
      <button
        className="btn btn-ghost btn-icon"
        onClick={() => setZoomStep((z) => Math.max(0, z - 1))}
        disabled={zoomStep === 0}
        aria-label="Zoom out"
        title="Zoom out"
      >
        <Icon d={MINUS} />
      </button>
      <span className="zoom-level" aria-live="polite">
        {Math.round(zoom * 100)}%
      </span>
      <button
        className="btn btn-ghost btn-icon"
        onClick={() => setZoomStep((z) => Math.min(ZOOMS.length - 1, z + 1))}
        disabled={zoomStep === ZOOMS.length - 1}
        aria-label="Zoom in"
        title="Zoom in"
      >
        <Icon d={PLUS} />
      </button>
    </div>
  );

  function togglePlacing() {
    if (placing) stopPlacing();
    else {
      clearSelection();
      setShowChanges(false);
      setPlacing(true);
    }
  }

  // What the side panel (wide screens) or the bottom sheet (phones) shows. Nothing: the tools.
  const panel =
    checking !== null && doc.history[checking]?.areas?.length ? (
      <CheckPanel doc={doc} index={checking} busy={busy} onClose={() => setChecking(null)} onUndo={() => void undo()} />
    ) : showChanges ? (
      <ChangesPanel
        doc={doc}
        busy={busy}
        comparing={compare !== null}
        onClose={() => setShowChanges(false)}
        onShow={(k) => {
          setShowChanges(false);
          show(k);
        }}
        onCheck={(k) => {
          setShowChanges(false);
          setChecking(k);
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
          onWorking={setWorking}
          onAdded={(outcome) => {
            stopPlacing();
            onChange(outcome.document);
            lightUp(outcome.document, outcome.document.history.at(-1));
            report("Text added.", outcome);
            offerCheck(outcome);
          }}
        />
      ) : (
        <section className="panel" aria-label="Add text">
          <div className="panel-head">
            <h2 className="panel-title">Add text</h2>
            <button className="btn btn-ghost btn-icon" onClick={stopPlacing} aria-label="Stop adding text">
              <Icon d={CLOSE} />
            </button>
          </div>
          <p className="panel-lede">
            <span className="wide-only">Click</span>
            <span className="narrow-only">Tap</span> the page where the new text should start.
          </p>
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
        onWorking={setWorking}
        onDelete={() => void remove(picked)}
        onSelectMore={() => setAdding(true)}
      />
    ) : picked.length > 0 || adding ? (
      <SelectionPanel picked={picked} deleting={deleting} onDelete={() => void remove(picked)} onClose={clearSelection} />
    ) : null;

  return (
    <div
      className={`viewer${working || deleting ? " viewer--working" : ""}${showText ? " viewer--show-text" : ""}${placing ? " viewer--placing" : ""}${compare ? " viewer--comparing" : ""}`}
    >
      <header className="bar">
        <button
          className="btn btn-ghost btn-icon"
          onClick={onClose}
          aria-label="Close this PDF and open another"
          title="Close this PDF and open another"
        >
          <Icon d={CLOSE} />
        </button>
        <span className="bar-divider wide-only" aria-hidden="true" />
        <span className="bar-mark wide-only">
          <Mark size={22} />
        </span>
        <div className="doc-title">
          <span className="doc-name" title={doc.name}>
            {doc.name}
          </span>
          <span className="doc-meta">
            {doc.pageCount} {doc.pageCount === 1 ? "page" : "pages"}
            {applied > 0 ? ` · ${applied} ${applied === 1 ? "change" : "changes"}` : ""}
          </span>
        </div>
        <div className="bar-actions">
          <button
            className="btn btn-ghost btn-icon"
            onClick={() => void undo()}
            disabled={busy || !doc.canUndo}
            aria-label="Undo the last change"
            title="Undo"
          >
            <Icon d={UNDO} />
          </button>
          <button
            className="btn btn-ghost btn-icon"
            onClick={() => void redo()}
            disabled={busy || !doc.canRedo}
            aria-label="Redo the change undone last"
            title="Redo"
          >
            <Icon d={REDO} />
          </button>
          <ThemeSwitch className="wide-only" />
          <span className="bar-divider wide-only" aria-hidden="true" />
          <button
            className="btn btn-secondary wide-only"
            aria-pressed={showChanges}
            onClick={() => setShowChanges((v) => !v)}
            disabled={doc.history.length === 0}
            aria-label={`Changes (${applied})`}
          >
            Changes
            {applied > 0 && <span className="count">{applied}</span>}
          </button>
          <a className="btn btn-primary" href={downloadUrl(doc)} download={doc.name} aria-label="Download PDF">
            <Icon d={DOWNLOAD} />
            <span className="wide-only">Download</span>
          </a>
        </div>
      </header>

      <div className="workspace">
        <section className="canvas" aria-label="Pages">
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
                    key={i}
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
                    magnifier={magnifier}
                    flash={flash && { revision: flash.revision, rects: flash.rects.filter((r) => r[0] === i).map((r) => r.slice(1)) }}
                  />
                ))}
            </div>
          </main>

          <div className="canvas-zoom">{zoomControls}</div>
        </section>

        <aside className="dock">
          {notice && (
            <div className={`notice notice--${notice.tone}`} role={notice.tone === "error" ? "alert" : "status"}>
              <p>
                {notice.text}
                {notice.detail && <span className="notice-detail">{notice.detail}</span>}
              </p>
              <button className="btn btn-ghost btn-icon" onClick={() => setNotice(null)} aria-label="Dismiss">
                <Icon d={CLOSE} size={16} />
              </button>
            </div>
          )}
          {panel ?? (
            <>
              <HomePanel
                doc={doc}
                placing={placing}
                showText={showText}
                magnifier={magnifier}
                onToggleMagnifier={() => setMagnifier((v) => !v)}
                comparing={compare !== null}
                onAddText={togglePlacing}
                onToggleText={() => setShowText((v) => !v)}
                onShowChanges={() => setShowChanges(true)}
                onCompare={() => (compare ? setCompare(null) : startComparing())}
              />
              <nav className="toolbar" aria-label="Tools">
                {!compare && (
                  <button className="toolbar-item" aria-pressed={placing} onClick={togglePlacing}>
                    <Icon d={ADD_TEXT} />
                    <span>Add text</span>
                  </button>
                )}
                {!compare && (
                  <button className="toolbar-item" aria-pressed={showText} onClick={() => setShowText((v) => !v)}>
                    <Icon d={BOXES} />
                    <span>Show text</span>
                  </button>
                )}
                <button
                  className="toolbar-item"
                  onClick={() => setShowChanges(true)}
                  disabled={doc.history.length === 0}
                  aria-label={`Changes (${applied})`}
                >
                  <Icon d={CHANGES} />
                  <span>
                    Changes
                    {applied > 0 && <span className="count">{applied}</span>}
                  </span>
                </button>
                {zoomControls}
              </nav>
            </>
          )}
        </aside>
      </div>
    </div>
  );
}
