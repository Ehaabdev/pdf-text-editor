import { useEffect, useRef, useState } from "react";
import { ApiError, addColumn, addLine, addRow, deleteColumn, deleteRow, editLine, getTables, type DocInfo, type EditOutcome, type PageTable, type TextLine, type TextSpan } from "../api";
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

export type Change = "changed" | "deleted" | "added" | "row added" | "row deleted" | "column added" | "column deleted";

type Props = {
  doc: DocInfo;
  line: TextLine;
  page: number; // 0-based
  deleting: boolean;
  onClose: () => void;
  onEdited: (outcome: EditOutcome, change: Change) => void;
  onDelete: () => void;
  onSelectMore: () => void;
  /** Pick the line up to drag it (with `table`, the whole table it's in). */
  onMove: (table: boolean) => void;
  /** The line or table is being picked up. */
  moving?: boolean;
  /** The server is checking this panel's change. */
  onWorking?: (working: boolean) => void;
};

type Mode = "view" | "edit" | "add" | "row" | "column";

export default function LinePanel({ doc, line, page, deleting, onClose, onEdited, onDelete, onSelectMore, onMove, moving = false, onWorking }: Props) {
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

  // A line in a table with borders: its row can be added to or deleted.
  const [table, setTable] = useState<{ table: PageTable; row: number } | null>(null);
  const [cells, setCells] = useState<string[]>([]);
  const [rowBusy, setRowBusy] = useState(false);
  useEffect(() => {
    let alive = true;
    setTable(null);
    getTables(doc, page)
      .then((tables) => {
        for (const t of tables) {
          const row = t.rows.findIndex((r) => r.some((c) => c.lines.includes(line.id)));
          if (row >= 0 && alive) return setTable({ table: t, row });
        }
      })
      .catch(() => {
        // Without the table, its row can't be changed: the rest of the panel works.
      });
    return () => {
      alive = false;
    };
  }, [doc, page, line.id]);
  const rowCells = table ? table.table.rows[table.row] : [];
  // A column's name for its box: the text at its top (the table's first row), else its number.
  function heading(k: number): string {
    const top = (table?.table.rows[0]?.[k]?.text ?? "").trim();
    if (!top) return `Column ${k + 1}`;
    return top.length > 28 ? `${top.slice(0, 28)}…` : top;
  }
  const merged = rowCells.some((c) => c.merged);
  useEffect(() => {
    onWorking?.(rowBusy);
  }, [rowBusy, onWorking]);

  async function saveRow() {
    if (rowBusy || !table) return;
    setRowBusy(true);
    setError(null);
    try {
      onEdited(await addRow(doc, page, line, where, cells), "row added");
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Something went wrong. Try again.");
    } finally {
      setRowBusy(false);
    }
  }

  async function removeRow() {
    if (rowBusy || !table) return;
    setRowBusy(true);
    setError(null);
    try {
      onEdited(await deleteRow(doc, page, line), "row deleted");
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Something went wrong. Try again.");
    } finally {
      setRowBusy(false);
    }
  }

  // A new column: which side (on the page), and a box for each of its cells.
  const [side, setSide] = useState<"left" | "right">("right");
  const [colCells, setColCells] = useState<string[]>([]);
  const rowCount = table ? table.table.rows.length : 0;
  const clicked = table?.table.rows.flat().find((c) => c.lines.includes(line.id));
  // The rows where the new column gets a cell of its own: one beside each cell of the column
  // it's added to (beside a merged cell, its last column on the right, its first on the left),
  // as tall as that cell. A cell merged across the new column's place gets wider instead.
  function newCellRows(next: "left" | "right"): { rows: number[]; widened: number } {
    if (!table || !clicked) return { rows: [], widened: 0 };
    const k = next === "right" ? clicked.col + clicked.colspan - 1 : clicked.col;
    const edge = next === "right" ? k + 1 : k;
    const rows: number[] = [];
    let widened = 0;
    for (const c of table.table.rows.flat()) {
      if (c.col > k || k >= c.col + c.colspan) continue;
      if (c.col < edge && edge < c.col + c.colspan) widened += 1;
      else rows.push(c.row);
    }
    return { rows: rows.sort((a, b) => a - b), widened };
  }
  const newCells = newCellRows(side);
  // A row's name for its box: the text of its first cell (in the table's order), else its number.
  function rowLabel(r: number): string {
    const first = (table?.table.rows[r]?.find((c) => c.text.trim())?.text ?? "").trim();
    if (!first) return `Row ${r + 1}`;
    return first.length > 28 ? `${first.slice(0, 28)}…` : first;
  }

  function showColumn(next: "left" | "right") {
    setMode("column");
    setSide(next);
    setColCells(Array.from({ length: rowCount }, () => ""));
    setError(null);
  }

  async function saveColumn() {
    if (rowBusy || !table) return;
    setRowBusy(true);
    setError(null);
    try {
      onEdited(await addColumn(doc, page, line, side, colCells), "column added");
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Something went wrong. Try again.");
    } finally {
      setRowBusy(false);
    }
  }

  async function removeColumn() {
    if (rowBusy || !table) return;
    setRowBusy(true);
    setError(null);
    try {
      onEdited(await deleteColumn(doc, page, line), "column deleted");
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Something went wrong. Try again.");
    } finally {
      setRowBusy(false);
    }
  }

  function show(next: Mode, side: "below" | "above" = "below") {
    setMode(next);
    setWhere(side);
    setAsItem(true);
    setDraft(next === "add" ? "" : line.display);
    setCells(rowCells.map(() => ""));
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
  const rowTitle = where === "below" ? "Add row below" : "Add row above";

  if (mode === "column" && table) {
    const title = side === "left" ? "Add column left" : "Add column right";
    return (
      <section className="panel" aria-label={title}>
        <div className="panel-head">
          <h2 className="panel-title">{title}</h2>
          <button className="btn btn-ghost btn-icon" onClick={onClose} aria-label="Close">
            <Icon d={CLOSE} />
          </button>
        </div>
        <p className="panel-lede">
          Type each new cell’s text, or leave it empty. The new column looks like this one; the others give it room from
          their empty space (or the table grows, where the page has room beside it).
        </p>
        <form
          onSubmit={(e) => {
            e.preventDefault();
            void saveColumn();
          }}
        >
          <div className="column-cells">
            {newCells.rows.map((r, n) => (
              <label key={r} className="row-cell">
                <span className="row-cell-label" dir="auto">
                  {rowLabel(r)}
                </span>
                <input
                  className="line-edit"
                  dir="auto"
                  value={colCells[r] ?? ""}
                  disabled={rowBusy}
                  autoFocus={n === 0}
                  onChange={(e) => setColCells((now) => now.map((v, i) => (i === r ? e.target.value.replace(/[\r\n]+/g, " ") : v)))}
                  onKeyDown={(e) => {
                    if (e.key === "Escape") {
                      e.stopPropagation();
                      show("view");
                    }
                  }}
                />
              </label>
            ))}
          </div>
          <p className="edit-note">
            Each cell’s text stays on one line, in the style of the cell beside it.
            {newCells.widened > 0 &&
              (newCells.widened === 1
                ? " A merged cell across the new column’s place gets wider instead of getting a new cell."
                : " Merged cells across the new column’s place get wider instead of getting new cells.")}
          </p>
          {error && (
            <p className="error" role="alert">
              {error}
            </p>
          )}
          <div className="edit-actions">
            <button className="btn btn-primary" type="submit" disabled={rowBusy}>
              {rowBusy && <span className="spinner" aria-hidden="true" />}
              {rowBusy ? "Checking the change…" : "Add column"}
            </button>
            <button className="btn btn-secondary" type="button" disabled={rowBusy} onClick={() => show("view")}>
              Cancel
            </button>
          </div>
        </form>
      </section>
    );
  }

  if (mode === "row" && table) {
    return (
      <section className="panel" aria-label={rowTitle}>
        <div className="panel-head">
          <h2 className="panel-title">{rowTitle}</h2>
          <button className="btn btn-ghost btn-icon" onClick={onClose} aria-label="Close">
            <Icon d={CLOSE} />
          </button>
        </div>
        <p className="panel-lede">
          Type each cell’s text, or leave a cell empty. The new row looks like this one, and the rows below move down to
          make room.
        </p>
        <form
          onSubmit={(e) => {
            e.preventDefault();
            void saveRow();
          }}
        >
          <div className="row-cells" dir={table.table.rtl ? "rtl" : "ltr"}>
            {rowCells.map((_, k) => (
              <label key={k} className="row-cell">
                <span className="row-cell-label" dir="auto">
                  {heading(k)}
                </span>
                <input
                  className="line-edit"
                  dir="auto"
                  value={cells[k] ?? ""}
                  disabled={rowBusy}
                  autoFocus={k === 0}
                  onChange={(e) => setCells((now) => now.map((v, i) => (i === k ? e.target.value.replace(/[\r\n]+/g, " ") : v)))}
                  onKeyDown={(e) => {
                    if (e.key === "Escape") {
                      e.stopPropagation();
                      show("view");
                    }
                  }}
                />
              </label>
            ))}
          </div>
          <p className="edit-note">Each cell’s text stays on one line, in the style of its column. Totals aren’t recalculated.</p>
          {error && (
            <p className="error" role="alert">
              {error}
            </p>
          )}
          <div className="edit-actions">
            <button className="btn btn-primary" type="submit" disabled={rowBusy}>
              {rowBusy && <span className="spinner" aria-hidden="true" />}
              {rowBusy ? "Checking the change…" : "Add row"}
            </button>
            <button className="btn btn-secondary" type="button" disabled={rowBusy} onClick={() => show("view")}>
              Cancel
            </button>
          </div>
        </form>
      </section>
    );
  }
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
            <button className="btn btn-secondary" onClick={() => onMove(false)} disabled={deleting || moving}>
              {moving && <span className="spinner" aria-hidden="true" />}
              Move line
            </button>
            <button className="btn btn-delete" onClick={onDelete} disabled={deleting}>
              {deleting && <span className="spinner" aria-hidden="true" />}
              {deleting ? "Deleting…" : "Delete line"}
            </button>
            <button className="btn btn-ghost" onClick={onSelectMore} disabled={deleting}>
              Select more lines
            </button>
          </div>

          {table && (
            <div className="panel-section row-section">
              <h3 className="panel-subtitle">Table</h3>
              {merged ? (
                <p className="panel-note">This row has a cell merged with the row next to it: rows can’t be added or deleted here yet.</p>
              ) : (
                <div className="edit-actions">
                  <button className="btn btn-secondary" onClick={() => show("row", "below")} disabled={deleting || rowBusy}>
                    Add row below
                  </button>
                  <button className="btn btn-secondary" onClick={() => show("row", "above")} disabled={deleting || rowBusy}>
                    Add row above
                  </button>
                  <button className="btn btn-delete" onClick={() => void removeRow()} disabled={deleting || rowBusy}>
                    {rowBusy && <span className="spinner" aria-hidden="true" />}
                    {rowBusy ? "Deleting…" : "Delete row"}
                  </button>
                </div>
              )}
              <div className="edit-actions">
                <button className="btn btn-secondary" onClick={() => onMove(true)} disabled={deleting || rowBusy || moving}>
                  Move table
                </button>
                <button className="btn btn-secondary" onClick={() => showColumn("left")} disabled={deleting || rowBusy || moving}>
                  Add column left
                </button>
                <button className="btn btn-secondary" onClick={() => showColumn("right")} disabled={deleting || rowBusy || moving}>
                  Add column right
                </button>
                <button className="btn btn-delete" onClick={() => void removeColumn()} disabled={deleting || rowBusy || moving}>
                  Delete column
                </button>
              </div>
              {error && mode === "view" && (
                <p className="error" role="alert">
                  {error}
                </p>
              )}
            </div>
          )}
        </>
      )}
    </section>
  );
}
