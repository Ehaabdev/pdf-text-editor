import { CLOSE, Icon } from "./Icon";

type PlaceProps = {
  table: boolean;
  count: number;
  saving: boolean;
  error: string | null;
  onSave: () => void;
  onCancel: () => void;
};

/** Lines or a table being dragged on their page: this puts them there. */
export function MovePanel({ table, count, saving, error, onSave, onCancel }: PlaceProps) {
  const title = table ? "Move table" : count === 1 ? "Move line" : `Move ${count} lines`;
  return (
    <section className="panel" aria-label={title}>
      <div className="panel-head">
        <h2 className="panel-title">{title}</h2>
        <button className="btn btn-ghost btn-icon" onClick={onCancel} aria-label="Cancel" disabled={saving}>
          <Icon d={CLOSE} />
        </button>
      </div>
      <p className="panel-lede">
        Drag {table ? "the table" : count === 1 ? "the line" : "the lines"} where you want {table || count === 1 ? "it" : "them"} on this
        page, or <span className="wide-only">click</span>
        <span className="narrow-only">tap</span> a spot. A dashed line shows where {table || count === 1 ? "it was" : "they were"}; that
        place stays empty.
      </p>
      <p className="panel-note">
        The new place must be empty: nothing is moved out of the way.
        <span className="wide-only"> The arrow keys move it a point at a time (with Shift, ten).</span>
      </p>
      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}
      <div className="edit-actions">
        <button className="btn btn-primary" onClick={onSave} disabled={saving}>
          {saving && <span className="spinner" aria-hidden="true" />}
          {saving ? "Checking the change…" : "Put it here"}
        </button>
        <button className="btn btn-ghost" onClick={onCancel} disabled={saving}>
          Cancel
        </button>
      </div>
    </section>
  );
}

type TogetherProps = {
  message: string;
  busy: boolean;
  onTogether: () => void;
  onCancel: () => void;
};

/** The chosen lines are stored as one with others: offer to move them all. */
export function TogetherPanel({ message, busy, onTogether, onCancel }: TogetherProps) {
  return (
    <section className="panel" aria-label="Move together">
      <div className="panel-head">
        <h2 className="panel-title">Move them together?</h2>
        <button className="btn btn-ghost btn-icon" onClick={onCancel} aria-label="Cancel" disabled={busy}>
          <Icon d={CLOSE} />
        </button>
      </div>
      <p className="panel-lede">{message}</p>
      <div className="edit-actions">
        <button className="btn btn-primary" onClick={onTogether} disabled={busy}>
          {busy && <span className="spinner" aria-hidden="true" />}
          Move them together
        </button>
        <button className="btn btn-ghost" onClick={onCancel} disabled={busy}>
          Cancel
        </button>
      </div>
    </section>
  );
}
