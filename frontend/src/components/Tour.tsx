import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { CLOSE, Icon } from "./Icon";

/** One stop of the tour: what to say, and the parts of the screen it lights up. */
export type Step = { title: string; text: string; targets: () => (Element | null)[] };

const q = (selector: string) => document.querySelector(selector);
const nth = (selector: string, n: number) => document.querySelectorAll(selector)[n] ?? null;
const shown = (el: Element | null): el is Element => !!el && el.getClientRects().length > 0;

/** The editor's tour. Wide screens have the side panel and the magnifier; phones the toolbar. */
export function editorSteps(narrow: boolean): Step[] {
  const tool = (n: number) => (narrow ? nth(".toolbar-item", n) : nth(".home-panel .tool", n));
  return [
    {
      title: "Your PDF",
      text: narrow
        ? "Tap any line of text. A panel opens to change it, add a line next to it, or delete it. New text keeps the line's font, size and color."
        : "Click any line of text to select it. Shift-click to select more than one.",
      targets: () => [q(".page")],
    },
    {
      title: "The panel",
      text: "The selected line shows here, magnified from your page. Change it, add a line next to it, or delete it. New text keeps the line's font, size and color.",
      targets: () => (narrow ? [] : [q(".dock > .panel")]),
    },
    {
      title: "Add text anywhere",
      text: narrow ? "Turn this on, then tap where the new text should start." : "Turn this on, then click where the new text should start.",
      targets: () => [tool(0)],
    },
    {
      title: "Show editable text",
      text: "Outlines every line on the pages that you can change.",
      targets: () => [tool(1)],
    },
    {
      title: "Magnifier",
      text: "A glass lens follows your mouse and shows the print up close. Press M to turn it on or off.",
      targets: () => (narrow ? [] : [tool(2)]),
    },
    {
      title: "Pages",
      text: "A small picture of every page. Jump straight to the one you need.",
      targets: () => [q(".page-rail"), q(".page-chip")],
    },
    {
      title: "Undo and redo",
      text: narrow ? "Take back your last change, or put it back." : "Take back your last change, or put it back. Ctrl+Z and Ctrl+Shift+Z work too.",
      targets: () => [q('[aria-label="Undo the last change"]'), q('[aria-label="Redo the change undone last"]')],
    },
    {
      title: "Your changes",
      text: "Every change is listed here. Look at each one up close, compare the pages with the original, or go back to it.",
      targets: () => [narrow ? q('.toolbar-item[aria-label^="Changes"]') : q('.bar [aria-label^="Changes"]')],
    },
    {
      title: "Download",
      text: "When you're done, download your edited PDF. The file is deleted from the server an hour after you last use it. Open this guide again any time with the ? button.",
      targets: () => [q(".bar a.btn-primary")],
    },
  ];
}

type Box = { x: number; y: number; w: number; h: number };

/** A guided tour: each step lights up part of the screen and explains it in a glass card. */
export default function Tour({ steps: all, onClose }: { steps: Step[]; onClose: () => void }) {
  const dialog = useRef<HTMLDialogElement>(null);
  const card = useRef<HTMLDivElement>(null);
  const next = useRef<HTMLButtonElement>(null);
  // Only the steps whose parts are on screen now (worked out once the screen has settled).
  const [steps, setSteps] = useState<Step[]>([]);
  const [k, setK] = useState(0);
  const [spot, setSpot] = useState<Box | null>(null);
  const [at, setAt] = useState<{ left: number; top: number } | null>(null);
  // The latest close action, without re-running the effects below when it changes.
  const close = useRef(onClose);
  close.current = onClose;

  useLayoutEffect(() => {
    const ready = all.filter((s) => s.targets().some(shown));
    // Nothing to show (it shouldn't happen): never leave an empty tour over the screen.
    if (!ready.length) return close.current();
    setSteps(ready);
    const d = dialog.current;
    if (d && !d.open) d.showModal();
  }, [all]);

  const step = steps[k];

  // Light up the step's parts and put the card where it fits: below, above, beside, or at the foot.
  useLayoutEffect(() => {
    if (!step) return;
    const parts = step.targets().filter(shown);
    parts[0]?.scrollIntoView({ block: "nearest", inline: "nearest" });
    const place = () => {
      const vw = window.innerWidth;
      const vh = window.innerHeight;
      const m = 12;
      const gap = 14;
      const rects = step.targets().filter(shown).map((el) => el.getBoundingClientRect());
      if (!rects.length) return;
      const pad = 8;
      const x0 = Math.max(6, Math.min(...rects.map((r) => r.left)) - pad);
      const y0 = Math.max(6, Math.min(...rects.map((r) => r.top)) - pad);
      const x1 = Math.min(vw - 6, Math.max(...rects.map((r) => r.right)) + pad);
      const y1 = Math.min(vh - 6, Math.max(...rects.map((r) => r.bottom)) + pad);
      const s = { x: x0, y: y0, w: x1 - x0, h: y1 - y0 };
      setSpot(s);

      const c = card.current;
      if (!c) return;
      const cw = c.offsetWidth;
      const ch = c.offsetHeight;
      let left = s.x + s.w / 2 - cw / 2;
      let top: number;
      if (s.y + s.h + gap + ch <= vh - m) top = s.y + s.h + gap;
      else if (s.y - gap - ch >= m) top = s.y - gap - ch;
      else if (s.x + s.w + gap + cw <= vw - m) {
        left = s.x + s.w + gap;
        top = s.y + s.h / 2 - ch / 2;
      } else if (s.x - gap - cw >= m) {
        left = s.x - gap - cw;
        top = s.y + s.h / 2 - ch / 2;
      } else top = vh - ch - m;
      setAt({
        left: Math.max(m, Math.min(vw - cw - m, left)),
        top: Math.max(m, Math.min(vh - ch - m, top)),
      });
    };
    place();
    // Smooth scrolling may still be moving things: place again once it has settled.
    const later = setTimeout(place, 400);
    window.addEventListener("resize", place);
    window.addEventListener("scroll", place, true);
    next.current?.focus();
    return () => {
      clearTimeout(later);
      window.removeEventListener("resize", place);
      window.removeEventListener("scroll", place, true);
    };
  }, [step]);

  useEffect(() => {
    if (steps.length && !step) close.current();
  }, [steps, step]);

  const last = k === steps.length - 1;
  const forward = () => (last ? onClose() : setK(k + 1));
  const back = () => setK(Math.max(0, k - 1));

  return (
    <dialog
      ref={dialog}
      className="tour"
      aria-label="How it works"
      onCancel={(e) => {
        e.preventDefault();
        onClose();
      }}
      onKeyDown={(e) => {
        if (e.key === "ArrowRight") forward();
        else if (e.key === "ArrowLeft") back();
      }}
    >
      {spot && <div className="tour-spot" style={{ left: spot.x, top: spot.y, width: spot.w, height: spot.h }} aria-hidden="true" />}
      {step && (
        <div className={`tour-card${at ? " tour-card--placed" : ""}`} ref={card} style={at ?? undefined}>
          <div className="tour-dots" aria-hidden="true">
            {steps.map((_, n) => (
              <span key={n} className={n === k ? "tour-dot tour-dot--on" : "tour-dot"} />
            ))}
          </div>
          <h2 className="tour-title">{step.title}</h2>
          <p className="tour-text">{step.text}</p>
          <div className="tour-actions">
            <span className="tour-count">
              {k + 1} of {steps.length}
            </span>
            {k > 0 && (
              <button className="btn btn-ghost" onClick={back}>
                Back
              </button>
            )}
            <button ref={next} className="btn btn-primary" onClick={forward}>
              {last ? "Done" : "Next"}
            </button>
          </div>
          <button className="btn btn-ghost btn-icon tour-close" onClick={onClose} aria-label="Close the guide">
            <Icon d={CLOSE} size={16} />
          </button>
        </div>
      )}
    </dialog>
  );
}
