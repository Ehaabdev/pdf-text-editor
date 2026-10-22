import { useEffect, useLayoutEffect, useRef, useState } from "react";

/** One line of the sample page that the demo retypes: the month, in each language. */
type Edit = { dir: "ltr" | "rtl"; before: string; after: string; prefix: string; suffix: string };

const EDITS: Edit[] = [
  { dir: "ltr", prefix: "This Agreement is made on 21 ", before: "October", after: "November", suffix: " 2026," },
  { dir: "rtl", prefix: "أبرمت هذه الاتفاقية بتاريخ 21 ", before: "أكتوبر", after: "نوفمبر", suffix: " 2026" },
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

/** A sample page whose lines are selected and retyped under a glass loupe, in English then Arabic. */
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
        <p className="demo-title demo-title--ar" dir="rtl" lang="ar">
          اتفاقية تقديم خدمات
        </p>
        <div className="demo-para">
          <p {...target(0)}>
            {EDITS[0].prefix}
            {shown(0)}
            {EDITS[0].suffix}
          </p>
          <p className="demo-line">between Elite Services Co. (the Provider)</p>
          <p className="demo-line">and Al Noor Trading Est. (the Client).</p>
        </div>
        <div className="demo-table">
          <p>Term: twelve (12) months</p>
          <p dir="rtl" lang="ar">
            المدة: اثنا عشر شهراً
          </p>
          <p>Fees: SAR 4,542.50 per month</p>
          <p dir="rtl" lang="ar">
            الرسوم: ٤٬٥٤٢٫٥٠ ريال شهرياً
          </p>
          <p>Notices: legal@example.com</p>
          <p dir="rtl" lang="ar">
            الإشعارات: بالبريد الإلكتروني
          </p>
        </div>
        <div className="demo-para" dir="rtl" lang="ar">
          <p {...target(1)}>
            {EDITS[1].prefix}
            {shown(1)}
            {EDITS[1].suffix}
          </p>
          <p className="demo-line">بين شركة النخبة للخدمات (مقدم الخدمة)</p>
          <p className="demo-line">ومؤسسة النور التجارية (العميل).</p>
        </div>
        <div className="demo-signs">
          <p>For the Provider</p>
          <p dir="rtl" lang="ar">
            عن العميل
          </p>
        </div>
      </div>

      <div
        className={`demo-loupe${frame.selected ? " demo-loupe--on" : ""}`}
        dir={edit.dir}
        style={lensTop[frame.line] ? { top: lensTop[frame.line] } : undefined}
      >
        <span className="demo-loupe-text" dir={edit.dir} lang={edit.dir === "rtl" ? "ar" : undefined}>
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
