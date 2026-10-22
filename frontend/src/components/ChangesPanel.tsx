import type { DocInfo } from "../api";
import { CLOSE, Icon } from "./Icon";

type Props = {
  doc: DocInfo;
  busy: boolean;
  comparing: boolean;
  onClose: () => void;
  /** Look at one change: the page it's on, compared with the original. */
  onShow: (index: number) => void;
  /** A close look at one change, before and after. */
  onCheck: (index: number) => void;
  onCompare: () => void;
  onGoTo: (version: number) => void;
};

/** Every change made, in order. Undone ones stay listed (they can be redone) until the next change. */
export default function ChangesPanel({ doc, busy, comparing, onClose, onShow, onCheck, onCompare, onGoTo }: Props) {
  const changes = doc.history;
  const undone = changes.filter((c) => !c.applied).length;
  return (
    <section className="panel changes-panel" aria-label="Changes">
      <div className="panel-head">
        <h2 className="panel-title">{changes.length === 0 ? "No changes yet" : changes.length === 1 ? "1 change" : `${changes.length} changes`}</h2>
        <button className="btn btn-ghost btn-icon" onClick={onClose} aria-label="Close">
          <Icon d={CLOSE} />
        </button>
      </div>

      {changes.length === 0 ? (
        <p className="edit-note">Changes you make to this PDF are listed here, so you can check them and undo them.</p>
      ) : (
        <ol className="changes-list">
          {changes.map((c, k) => (
            <li key={k} className={c.applied ? undefined : "change--undone"}>
              <div className="change-text">
                <span>{c.summary ?? "An earlier change"}</span>
                <span className="change-meta">
                  {(c.pages ?? []).length === 0
                    ? ""
                    : c.pages.length === 1
                      ? `Page ${c.pages[0] + 1}`
                      : `Pages ${c.pages.map((p) => p + 1).join(", ")}`}
                  {c.applied ? "" : ", undone"}
                  {c.lookalikes?.length ? ", look-alike font" : ""}
                </span>
              </div>
              {c.applied && c.areas?.length > 0 && (
                <button className="btn btn-ghost" onClick={() => onCheck(k)} disabled={busy}>
                  Check
                </button>
              )}
              {c.applied && c.areas?.length > 0 && (
                <button className="btn btn-ghost" onClick={() => onShow(k)} disabled={busy}>
                  Show
                </button>
              )}
            </li>
          ))}
        </ol>
      )}

      <div className="edit-actions">
        {doc.version > 0 && (
          <button className="btn btn-secondary" aria-pressed={comparing} onClick={onCompare} disabled={busy}>
            {comparing ? "Stop comparing" : "Compare with the original"}
          </button>
        )}
        {doc.version > 0 && (
          <button className="btn btn-ghost" onClick={() => onGoTo(0)} disabled={busy}>
            Go back to the original
          </button>
        )}
        {undone > 0 && (
          <button className="btn btn-ghost" onClick={() => onGoTo(changes.length)} disabled={busy}>
            {undone === 1 ? "Redo the undone change" : `Redo all ${undone} undone changes`}
          </button>
        )}
      </div>
    </section>
  );
}
