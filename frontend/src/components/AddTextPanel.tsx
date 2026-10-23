import { useEffect, useMemo, useRef, useState } from "react";
import { ApiError, addLine, addTextAt, getLines, spotPlan, type DocInfo, type EditOutcome, type SpotPlan, type TextLine } from "../api";
import { fontLabel, pointSize, styleLabel } from "../fonts";
import { CLOSE, Icon } from "./Icon";

export type Spot = { page: number; x: number; y: number }; // page points, as displayed

type Props = {
  doc: DocInfo;
  spot: Spot;
  onClose: () => void;
  onAdded: (outcome: EditOutcome) => void;
  /** The server is checking the new text. */
  onWorking?: (working: boolean) => void;
  /** What adding at the spot will do, once known: the page shows where it goes. */
  onPlan?: (plan: SpotPlan | null) => void;
};

type Style = { key: string; line: TextLine; distance: number };

function distance(line: TextLine, x: number, y: number) {
  const [x0, y0, x1, y1] = line.bbox;
  const dx = Math.max(x0 - x, 0, x - x1);
  const dy = Math.max(y0 - y, 0, y - y1);
  return Math.hypot(dx, dy);
}

/** The text styles on the page, nearest to the spot first, each shown by its nearest line. */
function stylesNear(lines: TextLine[], spot: Spot): Style[] {
  const best = new Map<string, Style>();
  for (const line of lines) {
    if (!line.text.trim()) continue;
    const key = [line.font, Math.round(line.size * 2) / 2, line.color, line.bold, line.italic].join("|");
    const d = distance(line, spot.x, spot.y);
    const seen = best.get(key);
    if (!seen || d < seen.distance) best.set(key, { key, line, distance: d });
  }
  return [...best.values()].sort((a, b) => a.distance - b.distance);
}

const SHOWN = 4;

export default function AddTextPanel({ doc, spot, onClose, onAdded, onWorking, onPlan }: Props) {
  const [lines, setLines] = useState<TextLine[] | null>(null);
  const [choice, setChoice] = useState<string | null>(null);
  const [open, setOpen] = useState(false);
  const [more, setMore] = useState(false);
  const [draft, setDraft] = useState("");
  const [saving, setSaving] = useState(false);
  const [plan, setPlan] = useState<SpotPlan | null>(null);
  // In a list, the new line becomes its next item unless this is unticked.
  const [asItem, setAsItem] = useState(true);
  // A new line next to a line couldn't be made: placing the text exactly at the spot instead.
  const [offerSpot, setOfferSpot] = useState(false);

  // Tell the page while the server works on this change (and stop if the panel closes).
  useEffect(() => {
    onWorking?.(saving);
  }, [saving, onWorking]);
  useEffect(() => () => onWorking?.(false), [onWorking]);
  const [error, setError] = useState<string | null>(null);
  const box = useRef<HTMLTextAreaElement>(null);

  useEffect(() => {
    let alive = true;
    getLines(doc, spot.page)
      .then((p) => alive && setLines(p.lines))
      .catch(() => alive && setError("The page's text couldn't be read. Try again."));
    return () => {
      alive = false;
    };
  }, [doc, spot.page]);

  const styles = useMemo(() => (lines ? stylesNear(lines, spot) : []), [lines, spot]);
  // A new spot: the nearest style again, and what adding there will do.
  useEffect(() => {
    setChoice(null);
    setOpen(false);
    setError(null);
    setOfferSpot(false);
    setAsItem(true);
    setPlan(null);
    onPlan?.(null);
    box.current?.focus();
    let alive = true;
    spotPlan(doc, spot.page, spot.x, spot.y)
      .then((p) => {
        if (!alive) return;
        setPlan(p);
        onPlan?.(p);
      })
      .catch(() => alive && setPlan({ kind: "free", lineId: null, oldText: null, where: "below", marker: null, box: [spot.x, spot.y, spot.x, spot.y], label: "Your text goes where you clicked." }));
    return () => {
      alive = false;
    };
  }, [doc, spot.page, spot.x, spot.y, onPlan]);
  useEffect(() => () => onPlan?.(null), [onPlan]);

  const chosen = styles.find((s) => s.key === choice) ?? styles[0];
  // A new line next to a line takes that line's style; joined or placed text, a chosen one.
  const ref = plan && (plan.kind === "item" || plan.kind === "line") ? lines?.find((l) => l.id === plan.lineId) ?? null : null;
  const bullet = plan?.marker != null && plan.marker.length === 1 && !/[\p{L}\p{N}]/u.test(plan.marker);

  async function save(atSpot = false) {
    if (saving || !plan) return;
    if (!draft.trim()) {
      setError("Type the text to add.");
      return;
    }
    setSaving(true);
    setError(null);
    try {
      if (ref && !atSpot) {
        onAdded(await addLine(doc, spot.page, ref, draft, plan.where, plan.kind === "item" && asItem));
      } else if (chosen) {
        onAdded(await addTextAt(doc, spot.page, ref ?? chosen.line, draft, [spot.x, spot.y]));
      }
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Something went wrong. Try again.");
      // The next best thing: the text exactly where it was clicked, nothing moving.
      setOfferSpot(!!ref && !atSpot);
    } finally {
      setSaving(false);
    }
  }

  // The nearest style is chosen; the others show on request, to keep the panel short.
  const shown = !open ? (chosen ? [chosen] : []) : more ? styles : styles.slice(0, SHOWN);

  return (
    <section className="panel" aria-label="Add text">
      <div className="panel-head">
        <h2 className="panel-title">Add text</h2>
        <button className="btn btn-ghost btn-icon" onClick={onClose} aria-label="Stop adding text">
          <Icon d={CLOSE} />
        </button>
      </div>
      {/* What will happen, in plain words: the page shows where. */}
      <p className="panel-lede spot-plan" aria-live="polite">
        {plan ? plan.label : "Working out where it goes…"}
      </p>

      <form
        onSubmit={(e) => {
          e.preventDefault();
          void save();
        }}
      >
        <textarea
          ref={box}
          className="line-edit"
          dir="auto"
          value={draft}
          rows={2}
          spellCheck={false}
          placeholder="Type the new text"
          aria-label="Text to add"
          disabled={saving}
          onChange={(e) => setDraft(e.target.value.replace(/[\r\n]+/g, " "))}
          onKeyDown={(e) => {
            if (e.key === "Enter") {
              e.preventDefault();
              void save();
            } else if (e.key === "Escape") {
              e.stopPropagation();
              onClose();
            }
          }}
        />

        {plan?.kind === "item" && plan.marker && (
          <label className={`style-option item-option${asItem ? " style-option--on" : ""}`}>
            <input type="checkbox" checked={asItem} onChange={(e) => setAsItem(e.target.checked)} disabled={saving} />
            {bullet ? `Make it a list item (${plan.marker})` : `Make it item ${plan.marker}`}
            {!bullet && asItem && <span className="item-note">Any items after it are renumbered.</span>}
          </label>
        )}

        {ref ? (
          <p className="edit-note">
            Uses the font ({fontLabel(ref.font)}), size ({pointSize(ref.size)}) and color of “
            <span dir="auto">{ref.display.length > 32 ? `${ref.display.slice(0, 32)}…` : ref.display}</span>”.
          </p>
        ) : (
        <fieldset className="style-choice" disabled={saving}>
          <legend>In the style of</legend>
          {lines === null && !error && <p className="edit-note">Reading the page…</p>}
          {lines !== null && styles.length === 0 && (
            <p className="line-warning">This page has no text to take a style from.</p>
          )}
          {shown.map((s) => (
            <label key={s.key} className={`style-option${chosen?.key === s.key ? " style-option--on" : ""}`}>
              <input
                type="radio"
                name="style"
                checked={chosen?.key === s.key}
                onChange={() => {
                  setChoice(s.key);
                  setOpen(false);
                }}
              />
              <span className="style-option-text">
                <span>
                  {fontLabel(s.line.font)}, {pointSize(s.line.size)}
                  {s.line.bold || s.line.italic ? `, ${styleLabel(s.line.bold, s.line.italic).toLowerCase()}` : ""}{" "}
                  <span className="swatch" style={{ background: `#${s.line.color}` }} aria-hidden="true" />
                </span>
                <span className="style-option-sample" dir="auto">
                  like “{s.line.display.length > 32 ? `${s.line.display.slice(0, 32)}…` : s.line.display}”
                </span>
              </span>
            </label>
          ))}
          {!open && styles.length > 1 && (
            <button type="button" className="btn btn-ghost" onClick={() => setOpen(true)}>
              Change style
            </button>
          )}
          {open && styles.length > SHOWN && (
            <button type="button" className="btn btn-ghost" onClick={() => setMore((m) => !m)}>
              {more ? "Fewer styles" : `${styles.length - SHOWN} more styles`}
            </button>
          )}
        </fieldset>
        )}

        <p className="edit-note">
          <span className="wide-only">Click</span>
          <span className="narrow-only">Tap</span> somewhere else on the page to put it there instead.
        </p>
        {error && (
          <p className="error" role="alert">
            {error}
          </p>
        )}
        {offerSpot && (
          <button type="button" className="btn btn-secondary" disabled={saving} onClick={() => void save(true)}>
            Place it exactly where I clicked instead
          </button>
        )}
        <div className="edit-actions">
          <button className="btn btn-primary" type="submit" disabled={saving || !plan || (!ref && !chosen)}>
            {saving && <span className="spinner" aria-hidden="true" />}
            {saving ? "Checking the change…" : "Add text"}
          </button>
          <button className="btn btn-secondary" type="button" disabled={saving} onClick={onClose}>
            Cancel
          </button>
        </div>
      </form>
    </section>
  );
}
