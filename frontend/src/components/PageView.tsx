import { useEffect, useState } from "react";
import { getLines, pageImageUrl, type DocInfo, type PageLines, type TextLine } from "../api";

// Rendered widths the server is asked for. A fixed set keeps browser caching effective.
const WIDTHS = [480, 720, 960, 1280, 1600, 2000, 2400, 3200];

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
};

export default function PageView({ doc, index, width, selectedIds, onSelect, placing, spot, onPlace, version, outlines = [] }: Props) {
  const editable = version === undefined;
  const size = doc.pages[index];
  const [state, setState] = useState<"loading" | "ready" | "failed">("loading");
  const [attempt, setAttempt] = useState(0);
  const [text, setText] = useState<PageLines | null>(null);

  // Text boxes are fetched once the page image is on screen.
  useEffect(() => {
    if (state !== "ready" || text) return;
    let alive = true;
    getLines(doc, index)
      .then((t) => alive && setText(t))
      .catch(() => {
        // Without text boxes the page still shows; nothing to tell the user here.
      });
    return () => {
      alive = false;
    };
  }, [state, text, doc, index]);

  const url = (w: number) => pageImageUrl(doc, index, w, version) + (attempt ? `&retry=${attempt}` : "");
  const pct = (v: number, of: number) => `${(v / of) * 100}%`;

  return (
    <figure className="page-wrap" id={`page-${index}`} style={{ width }}>
      <div
        className={`page page--${state}`}
        style={{ aspectRatio: `${size.width} / ${size.height}` }}
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
        <img
          key={`${attempt}-${version ?? "now"}`}
          src={url(960)}
          srcSet={WIDTHS.map((w) => `${url(w)} ${w}w`).join(", ")}
          sizes={`${Math.round(width)}px`}
          alt={`Page ${index + 1}`}
          loading={index < 2 ? "eager" : "lazy"}
          decoding="async"
          draggable={false}
          onLoad={() => setState("ready")}
          onError={() => setState("failed")}
        />
        {outlines.map(([x0, y0, x1, y1], k) => (
          <span
            key={k}
            className="change-outline"
            style={{ left: pct(x0, size.width), top: pct(y0, size.height), width: pct(x1 - x0, size.width), height: pct(y1 - y0, size.height) }}
            aria-hidden="true"
          />
        ))}
        {text && editable && (
          <div className="page-overlay">
            {text.lines.map((line) => {
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
                    left: pct(x0 - padX, text.width),
                    top: pct(y0 - padY, text.height),
                    width: pct(x1 - x0 + 2 * padX, text.width),
                    height: pct(y1 - y0 + 2 * padY, text.height),
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
                setState("loading");
                setAttempt((a) => a + 1);
              }}
            >
              Try again
            </button>
          </div>
        )}
      </div>
      <figcaption>
        Page {index + 1} of {doc.pageCount}
      </figcaption>
    </figure>
  );
}
