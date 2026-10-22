import { areaImageUrl, type DocInfo } from "../api";
import { CLOSE, Icon } from "./Icon";

type Props = {
  doc: DocInfo;
  /** Which change to look at: doc.history[index] made version index + 1. */
  index: number;
  busy: boolean;
  onClose: () => void;
  onUndo: () => void;
};

const PAD_X = 24;
const PAD_Y = 10;

/** A close look at one change, before and after, with what to check. */
export default function CheckPanel({ doc, index, busy, onClose, onUndo }: Props) {
  const change = doc.history[index];
  if (!change) return null;
  const [page, x0, y0, x1, y1] = (change.marks ?? change.areas)[0];
  const size = doc.pages[page];
  const area = [Math.max(0, x0 - PAD_X), Math.max(0, y0 - PAD_Y), Math.min(size.width, x1 + PAD_X), Math.min(size.height, y1 + PAD_Y)];
  const latest = change.applied && doc.version === index + 1;
  const lookalikes = change.lookalikes ?? [];

  return (
    <section className="panel check-panel" aria-label="Check the change">
      <div className="panel-head">
        <h2 className="panel-title">Check the change</h2>
        <button className="btn btn-ghost btn-icon" onClick={onClose} aria-label="Close">
          <Icon d={CLOSE} />
        </button>
      </div>

      <p className="check-summary">{change.summary}</p>
      {lookalikes.map((l, k) => (
        <p key={k} className="line-warning">
          {l.font} isn’t available here, so the new words use {l.substitute}
          {l.sameWidths ? ", a look-alike with nearly the same letter widths." : ", a similar font. Its letters are a little different."} Does
          it look right?
        </p>
      ))}

      <figure className="check-shot">
        <figcaption>Before</figcaption>
        <img src={areaImageUrl(doc, page, index, area, 900)} alt="The spot before the change" />
      </figure>
      <figure className="check-shot">
        <figcaption>After</figcaption>
        <img src={areaImageUrl(doc, page, index + 1, area, 900)} alt="The spot after the change" />
      </figure>

      <div className="edit-actions">
        <button className="btn btn-primary" onClick={onClose} disabled={busy}>
          {lookalikes.length ? "Keep it" : "Close"}
        </button>
        {latest && (
          <button className="btn btn-secondary" onClick={onUndo} disabled={busy}>
            Undo this change
          </button>
        )}
      </div>
      {!latest && change.applied && <p className="edit-note">Later changes came after this one. Undo them first to undo it.</p>}
    </section>
  );
}
