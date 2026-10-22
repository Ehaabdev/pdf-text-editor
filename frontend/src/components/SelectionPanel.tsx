import type { PickedLine } from "../api";

type Props = {
  picked: PickedLine[];
  deleting: boolean;
  onDelete: () => void;
  onClose: () => void;
};

const SHOWN = 4;

/** Several lines picked at once (Shift-click, or "Select more lines" on a phone). */
export default function SelectionPanel({ picked, deleting, onDelete, onClose }: Props) {
  const n = picked.length;
  const pages = new Set(picked.map((p) => p.page));
  return (
    <section className="line-panel" aria-label="Selected lines">
      <div className="line-panel-head">
        <h2>{n === 0 ? "Select lines" : n === 1 ? "1 line selected" : `${n} lines selected`}</h2>
        <button className="btn btn-ghost btn-icon" onClick={onClose} aria-label="Clear the selection">
          <svg viewBox="0 0 20 20" width="18" height="18" aria-hidden="true">
            <path d="M5 5l10 10M15 5L5 15" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" />
          </svg>
        </button>
      </div>

      {n === 0 ? (
        <p className="edit-note">Tap the lines you want. Tap a line again to leave it out.</p>
      ) : (
        <>
          <ul className="picked-list">
            {picked.slice(0, SHOWN).map(({ page, line }) => (
              <li key={`${page}:${line.id}`} dir={line.dir} lang={line.dir === "rtl" ? "ar" : undefined}>
                {line.display}
              </li>
            ))}
          </ul>
          {n > SHOWN && <p className="edit-note">and {n - SHOWN} more</p>}
          <p className="edit-note">
            Tap more lines to add them, or a selected line to leave it out
            {pages.size > 1 ? `. The lines are on ${pages.size} pages` : ""}.
          </p>
        </>
      )}

      <div className="edit-actions">
        <button className="btn btn-delete" onClick={onDelete} disabled={n === 0 || deleting}>
          {deleting ? "Deleting…" : n === 1 ? "Delete line" : n > 1 ? `Delete ${n} lines` : "Delete lines"}
        </button>
        <button className="btn btn-secondary" onClick={onClose} disabled={deleting}>
          Cancel
        </button>
      </div>
    </section>
  );
}
