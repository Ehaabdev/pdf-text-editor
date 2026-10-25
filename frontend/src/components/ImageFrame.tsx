import { useRef } from "react";

type Box = [number, number, number, number];

type Props = {
  /** Where the picture is, in points as displayed. */
  box: Box;
  /** The page's size in points. */
  page: { width: number; height: number };
  /** The picture shown in the frame. */
  src: string;
  onChange: (box: Box) => void;
};

const CORNERS = ["nw", "ne", "sw", "se"] as const;
const MIN = 8; // points

/** A picture being placed: drag it to move it, drag a corner to resize it (its shape
 *  kept). Works with a mouse and with a finger; it stays on the page. */
export default function ImageFrame({ box, page, src, onChange }: Props) {
  const frame = useRef<HTMLDivElement>(null);
  const start = useRef<{ x: number; y: number; box: Box; corner: (typeof CORNERS)[number] | null; scale: number } | null>(null);

  function down(e: React.PointerEvent, corner: (typeof CORNERS)[number] | null) {
    e.stopPropagation();
    e.preventDefault();
    const pageEl = frame.current?.parentElement;
    if (!pageEl) return;
    const scale = page.width / pageEl.getBoundingClientRect().width; // points per screen pixel
    start.current = { x: e.clientX, y: e.clientY, box, corner, scale };
    (e.currentTarget as HTMLElement).setPointerCapture(e.pointerId);
  }

  function move(e: React.PointerEvent) {
    const s = start.current;
    if (!s) return;
    const dx = (e.clientX - s.x) * s.scale;
    const dy = (e.clientY - s.y) * s.scale;
    const [x0, y0, x1, y1] = s.box;
    const w = x1 - x0;
    const h = y1 - y0;
    if (!s.corner) {
      // Moved, kept on the page.
      const nx = Math.min(Math.max(x0 + dx, 0), page.width - w);
      const ny = Math.min(Math.max(y0 + dy, 0), page.height - h);
      onChange([nx, ny, nx + w, ny + h]);
      return;
    }
    // Resized from a corner, the opposite corner staying, the shape kept.
    const east = s.corner.endsWith("e");
    const south = s.corner.startsWith("s");
    const grow = Math.max((east ? dx : -dx) / w, (south ? dy : -dy) / h);
    let nw = Math.max(MIN, w * (1 + grow));
    let nh = (nw * h) / w;
    const ax = east ? x0 : x1; // the corner that stays
    const ay = south ? y0 : y1;
    const roomX = east ? page.width - ax : ax;
    const roomY = south ? page.height - ay : ay;
    const fit = Math.min(1, roomX / nw, roomY / nh);
    nw *= fit;
    nh *= fit;
    const nx0 = east ? ax : ax - nw;
    const ny0 = south ? ay : ay - nh;
    onChange([nx0, ny0, nx0 + nw, ny0 + nh]);
  }

  function up(e: React.PointerEvent) {
    start.current = null;
    (e.currentTarget as HTMLElement).releasePointerCapture?.(e.pointerId);
  }

  const pct = (v: number, of: number) => `${(v / of) * 100}%`;
  return (
    <div
      ref={frame}
      className="image-frame"
      style={{
        left: pct(box[0], page.width),
        top: pct(box[1], page.height),
        width: pct(box[2] - box[0], page.width),
        height: pct(box[3] - box[1], page.height),
      }}
      onPointerDown={(e) => down(e, null)}
      onPointerMove={move}
      onPointerUp={up}
      onPointerCancel={up}
      onClick={(e) => e.stopPropagation()}
      role="img"
      aria-label="The picture: drag it to move it, drag a corner to resize it"
    >
      <img src={src} alt="" draggable={false} />
      {CORNERS.map((c) => (
        <span
          key={c}
          className={`image-handle image-handle--${c}`}
          onPointerDown={(e) => down(e, c)}
          onPointerMove={move}
          onPointerUp={up}
          onPointerCancel={up}
          aria-hidden="true"
        />
      ))}
    </div>
  );
}
