import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { downloadUrl, type DocInfo, type TextLine } from "../api";
import LinePanel from "./LinePanel";
import PageView from "./PageView";

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

type Selection = { page: number; line: TextLine } | null;

export default function Viewer({ doc, onClose }: { doc: DocInfo; onClose: () => void }) {
  const scroller = useRef<HTMLElement>(null);
  const available = useWidth(scroller);
  const [zoomStep, setZoomStep] = useState(0);
  const [showText, setShowText] = useState(false);
  const [selection, setSelection] = useState<Selection>(null);
  const zoom = ZOOMS[zoomStep];

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
      if (e.key === "Escape") setSelection(null);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const pageWidth = (ptWidth: number) =>
    Math.max(200, Math.min(available - 2 * GUTTER, ptWidth * CSS_PX_PER_PT)) * zoom;

  return (
    <div className={`desk viewer${showText ? " viewer--show-text" : ""}`}>
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

      <main className="pages" ref={scroller}>
        <div className="pages-inner">
          {available > 0 &&
            doc.pages.map((p, i) => (
              <PageView
                key={`${doc.revision}-${i}`}
                doc={doc}
                index={i}
                width={pageWidth(p.width)}
                selectedId={selection?.page === i ? selection.line.id : null}
                onSelect={(line) => setSelection(line ? { page: i, line } : null)}
              />
            ))}
        </div>
      </main>

      <div className="viewer-bottom">
      <div className="view-controls" role="toolbar" aria-label="View">
        <button
          className="btn btn-control"
          aria-pressed={showText}
          onClick={() => setShowText((v) => !v)}
          title={showText ? "Hide text boxes" : "Show all text boxes"}
          aria-label={showText ? "Hide text boxes" : "Show all text boxes"}
        >
          <Icon d={BOXES} />
        </button>
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

      {selection && <LinePanel line={selection.line} page={selection.page + 1} onClose={() => setSelection(null)} />}
      </div>
    </div>
  );
}
