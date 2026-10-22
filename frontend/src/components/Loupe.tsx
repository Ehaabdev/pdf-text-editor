import { useRef } from "react";
import { areaImageUrl, type DocInfo, type TextLine } from "../api";
import { useGlide } from "../glide";

// The line is shown this tall on screen (CSS px), whatever its size on the page.
const HEIGHT = 40;
// The server renders at most this many pixels wide.
const MAX_WIDTH = 3200;

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
  useGlide(scroller, line.dir, src);

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
