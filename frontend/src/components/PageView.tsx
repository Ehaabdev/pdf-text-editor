import { useEffect, useRef, useState } from "react";
import { getLines, pageImageUrl, type DocInfo, type PageLines, type TextLine } from "../api";

// Rendered widths the server is asked for. A fixed set keeps browser caching effective.
const WIDTHS = [480, 720, 960, 1280, 1600, 2000, 2400, 3200];
// The magnifier: how much larger it shows the page, and its radius (CSS px).
const MAG = 2.4;
const LENS = 92;

/** Spots to light up briefly once this page's new image is on screen: where a change was made. */
export type Flash = { revision: number; rects: number[][] };

type Props = {
  doc: DocInfo;
  index: number;
  /** Width of the page on screen, in CSS pixels. */
  width: number;
  selectedIds: Set<string>;
  /** A line was clicked; `more` when it should be added to the selection (Shift or Ctrl). */
  onSelect: (line: TextLine | null, more: boolean) => void;
  /** Adding text: a tap anywhere on the page puts the mark there (page points, as displayed). */
  placing: boolean;
  spot: { x: number; y: number } | null;
  onPlace: (x: number, y: number) => void;
  /** Comparing: which stored version to show (0 is the original), with editing off. */
  version?: number;
  /** Areas to outline (points, as displayed): where changes were made. */
  outlines?: number[][];
  flash?: Flash | null;
  /** A round lens follows the mouse over the page, showing the print larger. */
  magnifier?: boolean;
};

/** One image of the page: which revision and version it shows, and its URLs. */
type Layer = { key: string; revision: number; src: string; srcSet: string };

export default function PageView({ doc, index, width, selectedIds, onSelect, placing, spot, onPlace, version, outlines = [], flash, magnifier }: Props) {
  const editable = version === undefined;
  const size = doc.pages[index];
  const [attempt, setAttempt] = useState(0);
  const [failed, setFailed] = useState<string | null>(null);
  // The image on screen. After a change the old image stays until the new one has
  // loaded, so the page never flashes blank.
  const [shown, setShown] = useState<Layer | null>(null);
  const [text, setText] = useState<{ revision: number; lines: PageLines } | null>(null);

  const url = (w: number) => pageImageUrl(doc, index, w, version) + (attempt ? `&retry=${attempt}` : "");
  const wanted: Layer = {
    key: `${doc.revision}-${version ?? "now"}-${attempt}`,
    revision: doc.revision,
    src: url(960),
    srcSet: WIDTHS.map((w) => `${url(w)} ${w}w`).join(", "),
  };
  const loaded = shown !== null;
  const state = failed === wanted.key ? "failed" : loaded ? "ready" : "loading";

  // Text boxes are fetched once the page has first shown, and again after every change.
  useEffect(() => {
    if (!loaded || text?.revision === doc.revision) return;
    let alive = true;
    getLines(doc, index)
      .then((t) => alive && setText({ revision: doc.revision, lines: t }))
      .catch(() => {
        // Without text boxes the page still shows; nothing to tell the user here.
      });
    return () => {
      alive = false;
    };
  }, [loaded, text, doc, index]);

  const lines = text?.revision === doc.revision ? text.lines : null;
  const pct = (v: number, of: number) => `${(v / of) * 100}%`;
  // While a new image loads underneath, the previous one stays on top of it.
  const swapping = shown !== null && shown.key !== wanted.key;
  const layers = swapping ? [wanted, shown] : [wanted];

  // The lens moves by direct style changes, not React state: the page has hundreds of line
  // boxes that shouldn't re-render on every mouse move.
  const lens = useRef<HTMLSpanElement>(null);
  const lensWidth = WIDTHS.find((w) => w >= width * MAG * Math.min(2, window.devicePixelRatio || 1)) ?? 3200;
  function moveLens(e: React.PointerEvent<HTMLDivElement>) {
    const el = lens.current;
    if (!el || e.pointerType !== "mouse") return;
    const r = e.currentTarget.getBoundingClientRect();
    const x = e.clientX - r.left;
    const y = e.clientY - r.top;
    // Like a phone's text magnifier, the lens sits just above the pointer, so the spot
    // being clicked stays in view.
    // The large image is fetched only for the page under the mouse, not for every page.
    const src = `url("${pageImageUrl(doc, index, lensWidth, version)}")`;
    if (el.dataset.src !== src) {
      el.dataset.src = src;
      el.style.backgroundImage = src;
    }
    el.style.transform = `translate(${x - LENS}px, ${y - 2 * LENS - 20}px)`;
    el.style.backgroundPosition = `${LENS - x * MAG}px ${LENS - y * MAG}px`;
    el.classList.add("lens--on");
  }

  return (
    // Pages rise into place one after another when the file opens (they stay mounted after).
    <figure className="page-wrap" id={`page-${index}`} style={{ width, "--delay": `${Math.min(index, 4) * 80}ms` } as React.CSSProperties}>
      <div
        className={`page page--${state}`}
        style={{ aspectRatio: `${size.width} / ${size.height}` }}
        onPointerMove={magnifier ? moveLens : undefined}
        onPointerLeave={() => lens.current?.classList.remove("lens--on")}
        onClick={(e) => {
          if (!editable) return;
          if (placing) {
            const r = e.currentTarget.getBoundingClientRect();
            onPlace(((e.clientX - r.left) / r.width) * size.width, ((e.clientY - r.top) / r.height) * size.height);
            return;
          }
          if (e.target === e.currentTarget || (e.target as HTMLElement).classList.contains("page-overlay")) onSelect(null, false);
        }}
      >
        {layers.map((layer) => (
          <img
            key={layer.key}
            className={layer !== wanted ? "page-prev" : undefined}
            src={layer.src}
            srcSet={layer.srcSet}
            sizes={`${Math.round(width)}px`}
            alt={layer === wanted ? `Page ${index + 1}` : ""}
            loading={index < 2 ? "eager" : "lazy"}
            decoding="async"
            draggable={false}
            onLoad={() => {
              if (layer.key !== wanted.key) return;
              setShown(layer);
              setFailed(null);
            }}
            onError={() => layer.key === wanted.key && setFailed(layer.key)}
          />
        ))}
        {outlines.map(([x0, y0, x1, y1], k) => (
          <span
            key={k}
            className="change-outline"
            style={{ left: pct(x0, size.width), top: pct(y0, size.height), width: pct(x1 - x0, size.width), height: pct(y1 - y0, size.height) }}
            aria-hidden="true"
          />
        ))}
        {/* The glow starts only once the new image is the one on screen. */}
        {flash &&
          shown?.revision === flash.revision &&
          flash.rects.map(([x0, y0, x1, y1], k) => (
            <span
              key={`${flash.revision}-${k}`}
              className="change-flash"
              style={{ left: pct(x0, size.width), top: pct(y0, size.height), width: pct(x1 - x0, size.width), height: pct(y1 - y0, size.height) }}
              aria-hidden="true"
            />
          ))}
        {lines && editable && (
          <div className="page-overlay">
            {lines.lines.map((line) => {
              const [x0, y0, x1, y1] = line.bbox;
              // A little extra around each line makes small text easier to tap.
              const padX = 1, padY = Math.min(2, (y1 - y0) * 0.15);
              const selected = selectedIds.has(line.id);
              return (
                <button
                  key={line.id}
                  type="button"
                  className={`line-box${selected ? " line-box--selected" : ""}${line.uncertain ? " line-box--uncertain" : ""}`}
                  style={{
                    left: pct(x0 - padX, lines.width),
                    top: pct(y0 - padY, lines.height),
                    width: pct(x1 - x0 + 2 * padX, lines.width),
                    height: pct(y1 - y0 + 2 * padY, lines.height),
                  }}
                  aria-label={line.text}
                  aria-pressed={selected}
                  onClick={(e) => {
                    e.stopPropagation();
                    onSelect(line, e.shiftKey || e.ctrlKey || e.metaKey);
                  }}
                />
              );
            })}
          </div>
        )}
        {spot && (
          <span className="spot" style={{ left: pct(spot.x, size.width), top: pct(spot.y, size.height) }} aria-hidden="true" />
        )}
        {state === "failed" && (
          <div className="page-failed">
            <p>This page didn't load.</p>
            <button
              className="btn btn-secondary"
              onClick={() => {
                setFailed(null);
                setAttempt((a) => a + 1);
              }}
            >
              Try again
            </button>
          </div>
        )}
      </div>
      {magnifier && (
        <span
          ref={lens}
          className="lens"
          style={{
            width: 2 * LENS,
            height: 2 * LENS,
            backgroundSize: `${width * MAG}px auto`,
          }}
          aria-hidden="true"
        />
      )}
      <figcaption>
        Page {index + 1} of {doc.pageCount}
      </figcaption>
    </figure>
  );
}
