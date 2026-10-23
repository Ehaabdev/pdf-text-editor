import { useEffect, useRef, useState } from "react";
import { ApiError, addLine, editLine, type DocInfo, type EditOutcome, type TextLine, type TextSpan } from "../api";
import { fontLabel, pointSize, styleLabel } from "../fonts";
import { CLOSE, Icon } from "./Icon";
import Loupe from "./Loupe";

type Look = Pick<TextSpan, "font" | "size" | "color" | "bold" | "italic">;

function sameLook(a: Look, b: Look) {
  return a.font === b.font && Math.abs(a.size - b.size) < 0.25 && a.color === b.color && a.bold === b.bold && a.italic === b.italic;
}

function Swatch({ color }: { color: string }) {
  return <span className="swatch" style={{ background: `#${color}` }} aria-hidden="true" />;
}

export type Change = "changed" | "deleted" | "added";

type Props = {
  doc: DocInfo;
  line: TextLine;
  page: number; // 0-based
  deleting: boolean;
  onClose: () => void;
  onEdited: (outcome: EditOutcome, change: Change) => void;
  onDelete: () => void;
  onSelectMore: () => void;
  /** The server is checking this panel's change. */
  onWorking?: (working: boolean) => void;
};

type Mode = "view" | "edit" | "add";

export default function LinePanel({ doc, line, page, deleting, onClose, onEdited, onDelete, onSelectMore, onWorking }: Props) {
  const [mode, setMode] = useState<Mode>("view");
  const [draft, setDraft] = useState(line.display);
  const [where, setWhere] = useState<"below" | "above">("below");
  // In a list, a new line becomes the list's next item unless this is unticked.
  const [asItem, setAsItem] = useState(true);
  const [saving, setSaving] = useState(false);

  // Tell the page while the server works on this change (and stop if the panel closes).
  useEffect(() => {
    onWorking?.(saving);
  }, [saving, onWorking]);
  useEffect(() => () => onWorking?.(false), [onWorking]);
  const [error, setError] = useState<string | null>(null);
  const box = useRef<HTMLTextAreaElement>(null);

  function show(next: Mode, side: "below" | "above" = "below") {
    setMode(next);
    setWhere(side);
    setAsItem(true);
    setDraft(next === "add" ? "" : line.display);
    setError(null);
  }

  // A different line was selected: start over.
  useEffect(() => {
    setMode("view");
    setDraft(line.display);
    setError(null);
  }, [line.id, line.display]);

  useEffect(() => {
    if (mode !== "view") {
      box.current?.focus();
      box.current?.select();
    }
  }, [mode]);

  // Other styles used in the line, besides the main one: a bold word, a colored word.
  const others: TextSpan[] = [];
  for (const s of line.spans) {
    if (s.text.trim() && !sameLook(s, line) && !others.some((o) => sameLook(o, s))) others.push(s);
  }

  async function save() {
    if (saving) return;
    if (mode === "add" && !draft.trim()) {
      setError("Type the text to add.");
      return;
    }
    setSaving(true);
    setError(null);
    try {
      if (mode === "add") onEdited(await addLine(doc, page, line, draft, where, asItem && !!marker), "added");
      else onEdited(await editLine(doc, page, line, draft), draft.trim() === "" ? "deleted" : "changed");
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Something went wrong. Try again.");
    } finally {
      setSaving(false);
    }
  }

  const style = `the font (${fontLabel(line.font)}), size (${pointSize(line.size)}) and color`;
  const addTitle = where === "below" ? "Add line below" : "Add line above";
  // The marker the new line gets as the list's next item, when the line is in a list.
  const marker = line.item?.[where] ?? null;
  const bullet = marker !== null && marker.length === 1 && !/[\p{L}\p{N}]/u.test(marker);

  return (
    <section className="panel" aria-label={mode === "edit" ? "Edit line" : mode === "add" ? addTitle : "Selected line"}>
      <div className="panel-head">
        <h2 className="panel-title">{mode === "edit" ? "Edit line" : mode === "add" ? addTitle : `Line on page ${page + 1}`}</h2>
        <button className="btn btn-ghost btn-icon" onClick={onClose} aria-label="Close">
          <Icon d={CLOSE} />
        </button>
      </div>

      <Loupe doc={doc} page={page} line={line} />

      {mode !== "view" ? (
        <form
          onSubmit={(e) => {
            e.preventDefault();
            void save();
          }}
        >
          <textarea
            ref={box}
            className="line-edit"
            dir={mode === "add" ? "auto" : line.dir}
            lang={line.dir === "rtl" ? "ar" : undefined}
            value={draft}
            rows={2}
            spellCheck={false}
            placeholder={mode === "add" ? "Type the new line" : undefined}
            aria-label={mode === "add" ? "Text of the new line" : "New text for this line"}
            disabled={saving}
            onChange={(e) => setDraft(e.target.value.replace(/[\r\n]+/g, " "))}
            onKeyDown={(e) => {
              if (e.key === "Enter") {
                e.preventDefault();
                void save();
              } else if (e.key === "Escape") {
                e.stopPropagation();
                show("view");
              }
            }}
          />
          {mode === "add" && marker && (
            <label className={`style-option item-option${asItem ? " style-option--on" : ""}`}>
              <input type="checkbox" checked={asItem} onChange={(e) => setAsItem(e.target.checked)} disabled={saving} />
              {bullet ? `Make it a list item (${marker})` : `Make it item ${marker}`}
              {!bullet && asItem && <span className="item-note">Any items after it are renumbered.</span>}
            </label>
          )}
          <p className="edit-note">
            {mode === "add"
              ? `Uses ${style} of this line. The text below moves down to make room.`
              : `Keeps ${style}.`}
          </p>
          {mode === "edit" && line.uncertain && (
            <p className="line-warning">
              Some letters on this line are stored wrongly in the PDF itself. Check the text above against the page and
              retype anything that differs.
            </p>
          )}
          {error && (
            <p className="error" role="alert">
              {error}
            </p>
          )}
          <div className="edit-actions">
            <button className="btn btn-primary" type="submit" disabled={saving}>
              {saving && <span className="spinner" aria-hidden="true" />}
              {saving ? "Checking the change…" : mode === "add" ? "Add line" : "Save change"}
            </button>
            <button className="btn btn-secondary" type="button" disabled={saving} onClick={() => show("view")}>
              Cancel
            </button>
          </div>
        </form>
      ) : (
        <>
          <p className="line-text" dir={line.dir} lang={line.dir === "rtl" ? "ar" : undefined}>
            {line.display}
          </p>

          {line.uncertain && (
            <p className="line-warning">
              Some letters on this line are stored wrongly in the PDF itself, so the text above may not match the page.
              Retype the whole line when you edit it.
            </p>
          )}

          <dl className="line-style">
            <div>
              <dt>Font</dt>
              <dd>{fontLabel(line.font)}</dd>
            </div>
            <div>
              <dt>Size</dt>
              <dd>{pointSize(line.size)}</dd>
            </div>
            <div>
              <dt>Color</dt>
              <dd>
                <Swatch color={line.color} />#{line.color.toUpperCase()}
              </dd>
            </div>
            <div>
              <dt>Style</dt>
              <dd>{styleLabel(line.bold, line.italic)}</dd>
            </div>
          </dl>

          {others.length > 0 && (
            <div className="line-others">
              <h3>Also on this line</h3>
              <ul>
                {others.map((s, i) => (
                  <li key={i}>
                    <span dir="auto">“{s.text.trim()}”</span> in {fontLabel(s.font)}, {pointSize(s.size)},{" "}
                    <Swatch color={s.color} />#{s.color.toUpperCase()}
                    {s.bold || s.italic ? `, ${styleLabel(s.bold, s.italic).toLowerCase()}` : ""}
                  </li>
                ))}
              </ul>
            </div>
          )}

          <div className="edit-actions">
            <button className="btn btn-primary" onClick={() => show("edit")} disabled={deleting}>
              Edit text
            </button>
            <button className="btn btn-secondary" onClick={() => show("add", "below")} disabled={deleting}>
              Add line below
            </button>
            <button className="btn btn-secondary" onClick={() => show("add", "above")} disabled={deleting}>
              Add line above
            </button>
            <button className="btn btn-delete" onClick={onDelete} disabled={deleting}>
              {deleting && <span className="spinner" aria-hidden="true" />}
              {deleting ? "Deleting…" : "Delete line"}
            </button>
            <button className="btn btn-ghost" onClick={onSelectMore} disabled={deleting}>
              Select more lines
            </button>
          </div>
        </>
      )}
    </section>
  );
}
