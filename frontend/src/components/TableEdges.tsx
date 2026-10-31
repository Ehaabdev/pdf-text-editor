import { useState } from "react";

type Props = {
  /** The page's size, in points as displayed. */
  page: { width: number; height: number };
  bbox: [number, number, number, number];
  /** The table's column edges, left to right, and how far each can go: [lowest, highest]. */
  cols: number[];
  limits: [number, number][];
  /** The whole table: how much smaller and bigger it can get, and its direction (an Arabic
   *  table grows from its top right corner). */
  scale: [number, number];
  rtl: boolean;
  busy: boolean;
  /** The column edges show while the pointer is over the table or a line of it is picked
   *  (and when one has the keyboard's focus); the corner handle always shows. */
  showEdges: boolean;
  onResize: (edge: number, x: number) => void;
  onScale: (width: number) => void;
};

/** Handles on a picked table's column edges. Drag one, or use the arrow keys and then Enter,
 *  to move it; a dotted line shows where it goes. It stops where a cell beside it would get
 *  too narrow for its text (cells don't wrap), or where the page has no more room.
 *  A handle at its bottom corner makes the whole table smaller or bigger, text included,
 *  like a picture; a dotted box shows its new size. */
export default function TableEdges({ page, bbox, cols, limits, scale, rtl, busy, showEdges, onResize, onScale }: Props) {
  const [drag, setDrag] = useState<{ edge: number; x: number } | null>(null);
  const [size, setSize] = useState<number | null>(null);
  const pct = (v: number, of: number) => `${(v / of) * 100}%`;
  const [x0, y0, x1, y1] = bbox;
  const w = x1 - x0;
  const h = y1 - y0;

  // A width in whole points, within the scale limits.
  function fit(width: number) {
    return Math.min(Math.floor(w * scale[1] + 1e-6), Math.max(Math.ceil(w * scale[0] - 1e-6), Math.round(width)));
  }

  // The width the corner handle asks for: from the table's other top corner to the pointer,
  // across and down (keeping the table's shape).
  function wanted(e: React.PointerEvent<HTMLElement>) {
    const area = (e.currentTarget.parentElement as HTMLElement).getBoundingClientRect();
    const px = ((e.clientX - area.left) / area.width) * page.width;
    const py = ((e.clientY - area.top) / area.height) * page.height;
    const across = rtl ? x1 - px : px - x0;
    const down = ((py - y0) / h) * w;
    return fit((across + down) / 2);
  }

  function finishSize(width: number) {
    setSize(null);
    if (Math.abs(width - w) >= 1) onScale(width);
  }
  const growable = Math.floor(w * scale[1] + 1e-6) - Math.ceil(w * scale[0] - 1e-6) >= 1;

  // Whole points from where the edge is, within its limits.
  function clamp(edge: number, x: number) {
    const [lo, hi] = limits[edge];
    return Math.min(hi, Math.max(lo, cols[edge] + Math.round(x - cols[edge])));
  }

  function at(e: React.PointerEvent<HTMLElement>, edge: number) {
    const area = (e.currentTarget.parentElement as HTMLElement).getBoundingClientRect();
    return clamp(edge, ((e.clientX - area.left) / area.width) * page.width);
  }

  function finish(edge: number, x: number) {
    setDrag(null);
    if (Math.abs(x - cols[edge]) >= 1) onResize(edge, x);
  }

  return (
    <div className="table-edges">
      {cols.map((x, edge) => {
        const [lo, hi] = limits[edge];
        const fixed = hi - lo < 1;
        const outer = edge === 0 || edge === cols.length - 1;
        return (
          <button
            key={edge}
            type="button"
            className={`table-edge${drag?.edge === edge ? " table-edge--active" : ""}${showEdges || drag ? "" : " table-edge--quiet"}`}
            disabled={busy || fixed}
            style={{ left: pct(x, page.width), top: pct(y0, page.height), height: pct(y1 - y0, page.height) }}
            aria-label={`${outer ? "Table edge" : "Column edge"} ${edge + 1} of ${cols.length}: drag it, or press the arrow keys and then Enter`}
            title={fixed ? "This edge can't move: the cells beside it have no room to spare." : outer ? "Drag to make the table wider or narrower" : "Drag to change the two columns' widths"}
            onPointerDown={(e) => {
              if (busy) return;
              e.preventDefault();
              e.stopPropagation();
              e.currentTarget.setPointerCapture(e.pointerId);
              setDrag({ edge, x: at(e, edge) });
            }}
            onPointerMove={(e) => {
              if (drag?.edge === edge) setDrag({ edge, x: at(e, edge) });
            }}
            onPointerUp={(e) => {
              if (drag?.edge === edge) finish(edge, at(e, edge));
            }}
            onPointerCancel={() => setDrag(null)}
            onClick={(e) => e.stopPropagation()}
            onBlur={() => drag?.edge === edge && setDrag(null)}
            onKeyDown={(e) => {
              if (e.key === "ArrowLeft" || e.key === "ArrowRight") {
                e.preventDefault();
                e.stopPropagation();
                const now = drag?.edge === edge ? drag.x : x;
                const step = (e.shiftKey ? 10 : 1) * (e.key === "ArrowLeft" ? -1 : 1);
                setDrag({ edge, x: clamp(edge, now + step) });
              } else if (e.key === "Enter" && drag?.edge === edge) {
                e.preventDefault();
                finish(edge, drag.x);
              } else if (e.key === "Escape" && drag) {
                e.stopPropagation();
                setDrag(null);
              }
            }}
          />
        );
      })}
      {drag && (
        <span
          className="table-edge-guide"
          style={{ left: pct(drag.x, page.width), top: pct(y0, page.height), height: pct(y1 - y0, page.height) }}
          aria-hidden="true"
        />
      )}
      <button
        type="button"
        className={`table-corner${rtl ? " table-corner--rtl" : ""}${size !== null ? " table-edge--active" : ""}`}
        disabled={busy || !growable}
        style={{ left: pct(rtl ? x0 : x1, page.width), top: pct(y1, page.height) }}
        aria-label="Table size: drag the corner, or press the arrow keys and then Enter, to make the whole table smaller or bigger"
        title={growable ? "Drag to make the whole table smaller or bigger, text included" : "This table can't change size here"}
        onPointerDown={(e) => {
          if (busy) return;
          e.preventDefault();
          e.stopPropagation();
          e.currentTarget.setPointerCapture(e.pointerId);
          setSize(wanted(e));
        }}
        onPointerMove={(e) => {
          if (size !== null) setSize(wanted(e));
        }}
        onPointerUp={(e) => {
          if (size !== null) finishSize(wanted(e));
        }}
        onPointerCancel={() => setSize(null)}
        onClick={(e) => e.stopPropagation()}
        onBlur={() => setSize(null)}
        onKeyDown={(e) => {
          const step = e.shiftKey ? 10 : 1;
          const grow = e.key === "ArrowRight" || e.key === "ArrowDown" ? 1 : e.key === "ArrowLeft" || e.key === "ArrowUp" ? -1 : 0;
          if (grow) {
            e.preventDefault();
            e.stopPropagation();
            setSize(fit((size ?? w) + grow * step * (rtl && (e.key === "ArrowLeft" || e.key === "ArrowRight") ? -1 : 1)));
          } else if (e.key === "Enter" && size !== null) {
            e.preventDefault();
            finishSize(size);
          } else if (e.key === "Escape" && size !== null) {
            e.stopPropagation();
            setSize(null);
          }
        }}
      />
      {size !== null && (
        <span
          className="table-size-guide"
          style={{
            left: pct(rtl ? x1 - size : x0, page.width),
            top: pct(y0, page.height),
            width: pct(size, page.width),
            height: pct((h * size) / w, page.height),
          }}
          aria-hidden="true"
        >
          <span className="table-size-label">{Math.round((size / w) * 100)}%</span>
        </span>
      )}
    </div>
  );
}
