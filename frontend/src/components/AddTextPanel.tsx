import { useEffect, useMemo, useRef, useState } from "react";
import { ApiError, addTextAt, getLines, type DocInfo, type EditOutcome, type TextLine } from "../api";
import { fontLabel, pointSize, styleLabel } from "../fonts";

export type Spot = { page: number; x: number; y: number }; // page points, as displayed

type Props = {
  doc: DocInfo;
  spot: Spot;
  onClose: () => void;
  onAdded: (outcome: EditOutcome) => void;
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

export default function AddTextPanel({ doc, spot, onClose, onAdded }: Props) {
  const [lines, setLines] = useState<TextLine[] | null>(null);
  const [choice, setChoice] = useState<string | null>(null);
  const [open, setOpen] = useState(false);
  const [more, setMore] = useState(false);
  const [draft, setDraft] = useState("");
  const [saving, setSaving] = useState(false);
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
  // A new spot: the nearest style again.
  useEffect(() => {
    setChoice(null);
    setOpen(false);
    setError(null);
    box.current?.focus();
  }, [spot.page, spot.x, spot.y]);

  const chosen = styles.find((s) => s.key === choice) ?? styles[0];

  async function save() {
    if (saving || !chosen) return;
    if (!draft.trim()) {
      setError("Type the text to add.");
      return;
    }
    setSaving(true);
    setError(null);
    try {
      onAdded(await addTextAt(doc, spot.page, chosen.line, draft, [spot.x, spot.y]));
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Something went wrong. Try again.");
    } finally {
      setSaving(false);
    }
  }

  // The nearest style is chosen; the others show on request, to keep the panel short.
  const shown = !open ? (chosen ? [chosen] : []) : more ? styles : styles.slice(0, SHOWN);

  return (
    <section className="line-panel" aria-label="Add text here">
      <div className="line-panel-head">
        <h2>Add text here</h2>
        <button className="btn btn-ghost btn-icon" onClick={onClose} aria-label="Stop adding text">
          <svg viewBox="0 0 20 20" width="18" height="18" aria-hidden="true">
            <path d="M5 5l10 10M15 5L5 15" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" />
          </svg>
        </button>
      </div>

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

        <p className="edit-note">
          The text starts at the mark: English runs right from it, Arabic runs left. Nothing on the page moves to make
          room. Tap elsewhere on the page to move the mark.
        </p>
        {error && (
          <p className="error" role="alert">
            {error}
          </p>
        )}
        <div className="edit-actions">
          <button className="btn btn-primary" type="submit" disabled={saving || !chosen}>
            {saving ? "Adding…" : "Add text"}
          </button>
          <button className="btn btn-secondary" type="button" disabled={saving} onClick={onClose}>
            Cancel
          </button>
        </div>
      </form>
    </section>
  );
}
