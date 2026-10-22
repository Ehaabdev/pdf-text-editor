import { useEffect, useLayoutEffect, useRef, useState } from "react";

/** A line of the sample page that the demo retypes: the words before and after the change. */
type Edit = { before: string; after: string; prefix: string; suffix: string };

const EDITS: Edit[] = [
  { prefix: "This Agreement is made on 21 ", before: "October", after: "November", suffix: " 2026," },
  { prefix: "Payment is due within ", before: "thirty (30)", after: "fifteen (15)", suffix: " days of each invoice." },
];

type Frame = { line: number; value: string; saved: boolean; selected: boolean };

// The whole loop as a list of frames and how long each one shows (ms).
function timeline(): [Frame, number][] {
  const frames: [Frame, number][] = [];
  EDITS.forEach((e, line) => {
    frames.push([{ line, value: e.before, saved: false, selected: false }, 900]);
    frames.push([{ line, value: e.before, saved: false, selected: true }, 800]);
    for (let n = e.before.length - 1; n >= 0; n--) frames.push([{ line, value: e.before.slice(0, n), saved: false, selected: true }, 70]);
    frames.push([{ line, value: "", saved: false, selected: true }, 250]);
    for (let n = 1; n <= e.after.length; n++) frames.push([{ line, value: e.after.slice(0, n), saved: false, selected: true }, 110]);
    frames.push([{ line, value: e.after, saved: false, selected: true }, 500]);
    frames.push([{ line, value: e.after, saved: true, selected: true }, 2400]);
  });
  return frames;
}

const FRAMES = timeline();
const STILL: Frame = { line: 0, value: EDITS[0].after, saved: true, selected: true };

function prefersStill() {
  return typeof window !== "undefined" && window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
}

/** A sample page whose lines are selected and retyped, one after the other, under a glass loupe. */
export default function Demo() {
  const [step, setStep] = useState(0);
  const [still] = useState(prefersStill);
  const stage = useRef<HTMLDivElement>(null);
  const targets = useRef<(HTMLParagraphElement | null)[]>([]);
  const [lensTop, setLensTop] = useState<number[]>([]);

  useEffect(() => {
    if (still) return;
    const t = setTimeout(() => setStep((s) => (s + 1) % FRAMES.length), FRAMES[step][1]);
    return () => clearTimeout(t);
  }, [step, still]);

  // The lens floats just under the line it magnifies, wherever the page's tilt puts it.
  useLayoutEffect(() => {
    const el = stage.current;
    if (!el) return;
    const place = () => {
      const box = el.getBoundingClientRect();
      setLensTop(targets.current.map((t) => (t ? t.getBoundingClientRect().bottom - box.top + box.width * 0.025 : 0)));
    };
    place();
    void document.fonts?.ready.then(place);
    const ro = new ResizeObserver(place);
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  // The page tilts toward the mouse (--tx, --ty from -1 to 1), as if held in the hand.
  useEffect(() => {
    const el = stage.current;
    if (!el || still || !window.matchMedia?.("(hover: hover) and (pointer: fine)").matches) return;
    let queued = false;
    let x = 0;
    let y = 0;
    const paint = () => {
      queued = false;
      const r = el.getBoundingClientRect();
      const clamp = (v: number) => Math.max(-1, Math.min(1, v));
      el.style.setProperty("--tx", clamp((x - (r.left + r.width / 2)) / r.width).toFixed(3));
      el.style.setProperty("--ty", clamp((y - (r.top + r.height / 2)) / r.height).toFixed(3));
    };
    const move = (e: PointerEvent) => {
      x = e.clientX;
      y = e.clientY;
      if (!queued) {
        queued = true;
        requestAnimationFrame(paint);
      }
    };
    window.addEventListener("pointermove", move, { passive: true });
    return () => window.removeEventListener("pointermove", move);
  }, [still]);

  const frame = still ? STILL : FRAMES[step][0];
  const edit = EDITS[frame.line];
  // A line shows its new month once saved, and keeps it until the loop starts over.
  const shown = (k: number) => (k < frame.line || (k === frame.line && frame.saved) ? EDITS[k].after : EDITS[k].before);
  const target = (k: number) => ({
    ref: (el: HTMLParagraphElement | null) => {
      targets.current[k] = el;
    },
    className: `demo-line${frame.line === k && frame.selected ? " demo-line--on" : ""}`,
  });

  return (
    <div className="demo" ref={stage} aria-hidden="true">
      <div className="demo-sheet demo-sheet--behind" />
      <div className="demo-sheet">
        <p className="demo-title">Service Agreement</p>
        <p className="demo-subtitle">Agreement No. SA-2026-114</p>
        <div className="demo-para">
          <p {...target(0)}>
            {EDITS[0].prefix}
            {shown(0)}
            {EDITS[0].suffix}
          </p>
          <p className="demo-line">between Elite Services Co. (the Provider)</p>
          <p className="demo-line">and Harbor Trading Co. (the Client).</p>
        </div>
        <div className="demo-table">
          <p>Term</p>
          <p>Twelve (12) months</p>
          <p>Monthly fee</p>
          <p>SAR 4,542.50</p>
          <p>Notices</p>
          <p>legal@example.com</p>
        </div>
        <div className="demo-para">
          <p {...target(1)}>
            {EDITS[1].prefix}
            {shown(1)}
            {EDITS[1].suffix}
          </p>
          <p className="demo-line">Late payments carry a fee of 1.5% per month.</p>
        </div>
        <div className="demo-signs">
          <p>For the Provider</p>
          <p>For the Client</p>
        </div>
      </div>

      <div
        className={`demo-loupe${frame.selected ? " demo-loupe--on" : ""}`}
        style={lensTop[frame.line] ? { top: lensTop[frame.line] } : undefined}
      >
        <span className="demo-loupe-text">
          {edit.prefix}
          {frame.value}
          {!frame.saved && <span className="demo-caret" />}
          {edit.suffix}
        </span>
      </div>

      <p className={`demo-check${frame.saved ? " demo-check--on" : ""}`}>
        <svg viewBox="0 0 20 20" width="16" height="16">
          <path d="M4.5 10.5l3.5 3.5 7.5-8" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />
        </svg>
        Same font. Nothing else on the page moved.
      </p>
    </div>
  );
}
