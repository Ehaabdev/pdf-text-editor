import { useEffect, useRef } from "react";
import { pageImageUrl, type DocInfo } from "../api";

type Props = {
  doc: DocInfo;
  /** The stored version the pages show (comparing), or the current one. */
  version?: number;
  /** The page in view. */
  current: number;
  onJump: (page: number) => void;
  className?: string;
};

/** A small picture of every page; the one in view is lit. Pictures load as they scroll into view. */
export default function Thumbnails({ doc, version, current, onJump, className = "" }: Props) {
  const list = useRef<HTMLOListElement>(null);

  // Keep the page in view's picture in view too, as the pages scroll.
  useEffect(() => {
    const item = list.current?.querySelector<HTMLElement>('[aria-current="page"]');
    const box = list.current;
    if (!item || !box) return;
    const r = item.getBoundingClientRect();
    const b = box.getBoundingClientRect();
    if (r.top < b.top) box.scrollTop -= b.top - r.top + 8;
    else if (r.bottom > b.bottom) box.scrollTop += r.bottom - b.bottom + 8;
  }, [current]);

  return (
    <ol className={`thumbs ${className}`} ref={list}>
      {doc.pages.map((p, i) => (
        <li key={i}>
          <button
            type="button"
            className="thumb"
            aria-current={i === current ? "page" : undefined}
            aria-label={`Go to page ${i + 1}`}
            onClick={() => onJump(i)}
          >
            <img
              src={pageImageUrl(doc, i, 240, version)}
              alt=""
              loading="lazy"
              decoding="async"
              draggable={false}
              style={{ aspectRatio: `${p.width} / ${p.height}` }}
            />
            <span className="thumb-no">{i + 1}</span>
          </button>
        </li>
      ))}
    </ol>
  );
}
