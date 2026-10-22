import { useEffect, useRef } from "react";
import { areaImageUrl, type DocInfo, type TextLine } from "../api";

// The line is shown this tall on screen (CSS px), whatever its size on the page.
const HEIGHT = 40;
// The server renders at most this many pixels wide.
const MAX_WIDTH = 3200;
// Gliding along a long line: speed (px per second) and the rest at each end (ms).
const SPEED = 36;
const REST = 1400;

/** The selected line, magnified from the page itself: the real font, as it is printed. */
export default function Loupe({ doc, page, line }: { doc: DocInfo; page: number; line: TextLine }) {
  const size = doc.pages[page];
  const [x0, y0, x1, y1] = line.bbox;
  const padY = (y1 - y0) * 0.3;
  const area = [
    Math.max(0, x0 - 6),
    Math.max(0, y0 - padY),
    Math.min(size.width, x1 + 6),
    Math.min(size.height, y1 + padY),
  ];
  const scale = HEIGHT / (area[3] - area[1]); // CSS px per point
  const width = Math.round((area[2] - area[0]) * scale);
  const density = Math.min(2, window.devicePixelRatio || 1);
  const src = areaImageUrl(doc, page, doc.version, area, Math.min(MAX_WIDTH, Math.round(width * density)));
  const scroller = useRef<HTMLDivElement>(null);

  // A line longer than the lens glides slowly from its start to its end and back, like a
  // loupe moved along it. Touching it (or asking for less motion) hands control to the reader.
  useEffect(() => {
    const el = scroller.current;
    if (!el || window.matchMedia?.("(prefers-reduced-motion: reduce)").matches) return;
    const sign = line.dir === "rtl" ? -1 : 1; // RTL scroll positions run negative
    let frame = 0;
    let at = 0;
    let towards = 1;
    let restUntil = performance.now() + REST;
    let last = performance.now();
    const tick = (now: number) => {
      const room = el.scrollWidth - el.clientWidth;
      const dt = now - last;
      last = now;
      if (room > 1 && now >= restUntil) {
        at += (towards * SPEED * dt) / 1000;
        if (at >= room || at <= 0) {
          at = Math.max(0, Math.min(room, at));
          towards = -towards;
          restUntil = now + REST;
        }
        el.scrollLeft = sign * at;
      }
      frame = requestAnimationFrame(tick);
    };
    frame = requestAnimationFrame(tick);
    const stop = () => cancelAnimationFrame(frame);
    el.addEventListener("pointerdown", stop);
    el.addEventListener("wheel", stop, { passive: true });
    el.addEventListener("touchstart", stop, { passive: true });
    return () => {
      stop();
      el.removeEventListener("pointerdown", stop);
      el.removeEventListener("wheel", stop);
      el.removeEventListener("touchstart", stop);
    };
  }, [src, line.dir]);

  return (
    <figure className="loupe">
      <div className="loupe-lens">
        {/* An Arabic line opens at its right-hand start. */}
        <div className="loupe-scroll" ref={scroller} dir={line.dir}>
          <img key={src} src={src} width={width} height={HEIGHT} alt="" draggable={false} />
        </div>
      </div>
      <figcaption>Magnified from your page</figcaption>
    </figure>
  );
}
