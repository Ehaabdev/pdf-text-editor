import { useEffect, useRef, useState } from "react";
import { pageImageUrl, type DocInfo } from "../api";
import { CLOSE, Icon, PLUS } from "./Icon";

/** Editing the pages from the list (not while comparing): each change goes to the server. */
export type PageEdits = {
  busy: boolean;
  onDelete: (page: number) => void;
  /** A new page at the end, like the last one. */
  onAdd: () => void;
  /** `to`: where the page ends up. */
  onMove: (page: number, to: number) => void;
};

type Props = {
  doc: DocInfo;
  /** The stored version the pages show (comparing), or the current one. */
  version?: number;
  /** The page in view. */
  current: number;
  onJump: (page: number) => void;
  className?: string;
  edit?: PageEdits;
};

/** A page being dragged: where it was picked up, how far it has moved, and the gap it
 *  would drop into (0 = before the first page, n = after the last). */
type Drag = { page: number; dx: number; dy: number; gap: number };

const HOLD_MS = 350; // touch: hold this long to pick a page up (shorter is a scroll or a tap)
const SLOP = 6; // mouse: move this far to start dragging (less is a click)

/** A small picture of every page; the one in view is lit. Pictures load as they scroll into
 *  view. With `edit`: an X on each to delete it, a + at the end to add one, and dragging
 *  (hold first on a touch screen) to move one; Alt+arrows and Delete from the keyboard. */
export default function Thumbnails({ doc, version, current, onJump, className = "", edit }: Props) {
  const list = useRef<HTMLOListElement>(null);
  const [drag, setDrag] = useState<Drag | null>(null);
  const press = useRef<{ page: number; x: number; y: number; id: number; touch: boolean; timer?: number; live: boolean; moved: boolean } | null>(null);
  // The click that ends a drag isn't a click on the page.
  const dragged = useRef(false);
  const editable = !!edit && version === undefined;
  const dragging = drag !== null;

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

  // While a page is dragged on a touch screen, the finger moves it, not the list.
  useEffect(() => {
    if (!dragging) return;
    const still = (e: TouchEvent) => e.preventDefault();
    window.addEventListener("touchmove", still, { passive: false });
    return () => window.removeEventListener("touchmove", still);
  }, [dragging]);

  /** The gap a point is over: before the first page whose middle it hasn't passed. */
  function gapAt(x: number, y: number): number {
    const items = Array.from(list.current?.querySelectorAll<HTMLElement>("[data-page]") ?? []);
    const grid = className.includes("thumbs--grid");
    for (let i = 0; i < items.length; i++) {
      const r = items[i].getBoundingClientRect();
      const before = grid ? y < r.top || (y <= r.bottom && x < r.left + r.width / 2) : y < r.top + r.height / 2;
      if (before) return i;
    }
    return items.length;
  }

  function start(page: number, e: React.PointerEvent<HTMLButtonElement>) {
    if (!editable || edit!.busy || doc.pageCount < 2 || (e.pointerType === "mouse" && e.button !== 0)) return;
    const touch = e.pointerType !== "mouse";
    press.current = { page, x: e.clientX, y: e.clientY, id: e.pointerId, touch, live: false, moved: false };
    if (touch) {
      press.current.timer = window.setTimeout(() => {
        const p = press.current;
        if (!p || p.moved) return;
        p.live = true;
        navigator.vibrate?.(10);
        setDrag({ page, dx: 0, dy: 0, gap: page });
      }, HOLD_MS);
    }
  }

  function move(e: React.PointerEvent<HTMLButtonElement>) {
    const p = press.current;
    if (!p || p.id !== e.pointerId) return;
    const dx = e.clientX - p.x;
    const dy = e.clientY - p.y;
    if (!p.live) {
      if (Math.hypot(dx, dy) < SLOP) return;
      p.moved = true;
      if (p.touch) return; // moved before the hold: the list scrolls
      p.live = true;
      e.currentTarget.setPointerCapture(e.pointerId);
    }
    if (p.touch && !e.currentTarget.hasPointerCapture(e.pointerId)) e.currentTarget.setPointerCapture(e.pointerId);
    setDrag({ page: p.page, dx, dy, gap: gapAt(e.clientX, e.clientY) });
    // Near the list's ends, it scrolls along.
    const box = list.current?.getBoundingClientRect();
    if (box && list.current) {
      if (e.clientY < box.top + 28) list.current.scrollTop -= 8;
      else if (e.clientY > box.bottom - 28) list.current.scrollTop += 8;
    }
  }

  function end(e: React.PointerEvent<HTMLButtonElement>, cancelled = false) {
    const p = press.current;
    if (!p || p.id !== e.pointerId) return;
    window.clearTimeout(p.timer);
    press.current = null;
    if (!p.live) return;
    dragged.current = true;
    setTimeout(() => (dragged.current = false), 0);
    const gap = drag?.gap ?? p.page;
    setDrag(null);
    const to = gap > p.page ? gap - 1 : gap;
    if (!cancelled && to !== p.page) edit!.onMove(p.page, to);
  }

  function keys(page: number, e: React.KeyboardEvent<HTMLButtonElement>) {
    if (!editable || edit!.busy) return;
    if (e.altKey && (e.key === "ArrowUp" || e.key === "ArrowLeft") && page > 0) {
      e.preventDefault();
      edit!.onMove(page, page - 1);
    } else if (e.altKey && (e.key === "ArrowDown" || e.key === "ArrowRight") && page < doc.pageCount - 1) {
      e.preventDefault();
      edit!.onMove(page, page + 1);
    } else if ((e.key === "Delete" || e.key === "Backspace") && doc.pageCount > 1) {
      e.preventDefault();
      edit!.onDelete(page);
    }
  }

  const last = doc.pages[doc.pages.length - 1];
  return (
    <ol className={`thumbs ${className}${drag ? " thumbs--dragging" : ""}${editable ? " thumbs--editable" : ""}`} ref={list} aria-busy={edit?.busy || undefined}>
      {doc.pages.map((p, i) => (
        <li
          key={i}
          data-page={i}
          className={`${drag?.page === i ? "thumb-lifted" : ""}${drag && drag.gap === i && drag.gap !== drag.page && drag.gap !== drag.page + 1 ? " thumb-gap-before" : ""}${drag && i === doc.pageCount - 1 && drag.gap === doc.pageCount && drag.page !== i ? " thumb-gap-after" : ""}`}
          style={drag?.page === i ? { transform: `translate(${drag.dx}px, ${drag.dy}px) scale(0.9)` } : undefined}
        >
          <button
            type="button"
            className="thumb"
            aria-current={i === current ? "page" : undefined}
            aria-label={`Go to page ${i + 1}`}
            aria-keyshortcuts={editable ? "Alt+ArrowUp Alt+ArrowDown Delete" : undefined}
            onClick={() => !dragged.current && onJump(i)}
            onPointerDown={(e) => start(i, e)}
            onPointerMove={move}
            onPointerUp={(e) => end(e)}
            onPointerCancel={(e) => end(e, true)}
            onKeyDown={(e) => keys(i, e)}
            onContextMenu={(e) => press.current?.touch && e.preventDefault()}
          >
            {version === 0 && i >= doc.originalPageCount ? (
              <span className="thumb-added" style={{ aspectRatio: `${p.width} / ${p.height}` }}>
                Added
              </span>
            ) : (
              <img
                src={pageImageUrl(doc, i, 240, version)}
                alt=""
                loading="lazy"
                decoding="async"
                draggable={false}
                style={{ aspectRatio: `${p.width} / ${p.height}` }}
              />
            )}
            <span className="thumb-no">{i + 1}</span>
          </button>
          {editable && doc.pageCount > 1 && (
            <button
              type="button"
              className="thumb-delete"
              aria-label={`Delete page ${i + 1}`}
              title={`Delete page ${i + 1}`}
              disabled={edit!.busy}
              onClick={() => edit!.onDelete(i)}
            >
              <Icon d={CLOSE} size={12} />
            </button>
          )}
        </li>
      ))}
      {editable && (
        <li>
          <button type="button" className="thumb thumb-add" onClick={edit!.onAdd} disabled={edit!.busy} aria-label="Add a page at the end">
            <span className="thumb-add-sheet" style={{ aspectRatio: `${last.width} / ${last.height}` }}>
              <Icon d={PLUS} size={20} />
            </span>
            <span className="thumb-no">Add page</span>
          </button>
        </li>
      )}
    </ol>
  );
}
