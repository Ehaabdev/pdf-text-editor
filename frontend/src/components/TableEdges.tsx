import { useState } from "react";

type Props = {
  /** The page's size, in points as displayed. */
  page: { width: number; height: number };
  bbox: [number, number, number, number];
  /** The table's column edges, left to right, and how far each can go: [lowest, highest]. */
  cols: number[];
  limits: [number, number][];
  busy: boolean;
  onResize: (edge: number, x: number) => void;
};

/** Handles on a picked table's column edges. Drag one, or use the arrow keys and then Enter,
 *  to move it; a dotted line shows where it goes. It stops where a cell beside it would get
 *  too narrow for its text (cells don't wrap), or where the page has no more room. */
export default function TableEdges({ page, bbox, cols, limits, busy, onResize }: Props) {
  const [drag, setDrag] = useState<{ edge: number; x: number } | null>(null);
  const pct = (v: number, of: number) => `${(v / of) * 100}%`;
  const [, y0, , y1] = bbox;

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
            className={`table-edge${drag?.edge === edge ? " table-edge--active" : ""}`}
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
    </div>
  );
}
