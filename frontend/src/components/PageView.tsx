import { useState } from "react";
import { pageImageUrl, type DocInfo } from "../api";

// Rendered widths the server is asked for. A fixed set keeps browser caching effective.
const WIDTHS = [480, 720, 960, 1280, 1600, 2000, 2400];
// On-screen size of a page on wide screens: an A4 page (595pt) shows about 860px wide.
const CSS_PX_PER_PT = 1.45;
const GUTTER = 16;

export default function PageView({ doc, index }: { doc: DocInfo; index: number }) {
  const size = doc.pages[index];
  const maxCss = Math.round(size.width * CSS_PX_PER_PT);
  const [state, setState] = useState<"loading" | "ready" | "failed">("loading");
  const [attempt, setAttempt] = useState(0);

  const url = (w: number) => pageImageUrl(doc, index, w) + (attempt ? `&retry=${attempt}` : "");

  return (
    <figure className="page-wrap" style={{ maxWidth: maxCss }}>
      <div className={`page page--${state}`} style={{ aspectRatio: `${size.width} / ${size.height}` }}>
        <img
          key={attempt}
          src={url(960)}
          srcSet={WIDTHS.map((w) => `${url(w)} ${w}w`).join(", ")}
          sizes={`(max-width: ${maxCss + GUTTER * 2}px) calc(100vw - ${GUTTER * 2}px), ${maxCss}px`}
          alt={`Page ${index + 1}`}
          loading={index < 2 ? "eager" : "lazy"}
          decoding="async"
          draggable={false}
          onLoad={() => setState("ready")}
          onError={() => setState("failed")}
        />
        {/* Editable text boxes are drawn here in a later step. */}
        <div className="page-overlay" />
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
