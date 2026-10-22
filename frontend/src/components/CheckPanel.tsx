import { useRef } from "react";
import { areaImageUrl, type DocInfo } from "../api";
import { useGlide } from "../glide";
import { CLOSE, Icon } from "./Icon";

type Props = {
  doc: DocInfo;
  /** Which change to look at: doc.history[index] made version index + 1. */
  index: number;
  busy: boolean;
  onClose: () => void;
  onUndo: () => void;
};

// Room around the change (points): some of the line's neighbours to the sides, and a
// little above and below (a third of a line), so the next line isn't cut in half.
const PAD_X = 24;
const PAD_Y = 0.35;
// How large the spot is drawn: about this tall for one line (CSS px), within these scales.
const STRIP = 110;
const MIN_SCALE = 1.4;
const MAX_SCALE = 3.2;
// The server renders at most this many pixels wide.
const MAX_WIDTH = 3200;

/** A close look at one change, before and after, magnified, with what to check. */
export default function CheckPanel({ doc, index, busy, onClose, onUndo }: Props) {
  const before = useRef<HTMLDivElement>(null);
  const after = useRef<HTMLDivElement>(null);
  const syncing = useRef(false);
  const change = doc.history[index];
  // An Arabic change opens at the line's right-hand start.
  const dir = change && /[֐-ࣿ]/.test(change.summary ?? "") ? "rtl" : "ltr";
  useGlide(before, dir, `${doc.revision}-${index}`);
  if (!change) return null;

  const [page, x0, y0, x1, y1] = (change.marks ?? change.areas)[0];
  const size = doc.pages[page];
  const padY = Math.max(3, Math.min(10, (y1 - y0) * PAD_Y));
  const area = [Math.max(0, x0 - PAD_X), Math.max(0, y0 - padY), Math.min(size.width, x1 + PAD_X), Math.min(size.height, y1 + padY)];
  const scale = Math.max(MIN_SCALE, Math.min(MAX_SCALE, STRIP / (area[3] - area[1]))); // CSS px per point
  const width = Math.round((area[2] - area[0]) * scale);
  const height = Math.round((area[3] - area[1]) * scale);
  const pixels = Math.min(MAX_WIDTH, Math.round(width * Math.min(2, window.devicePixelRatio || 1)));
  const latest = change.applied && doc.version === index + 1;
  const lookalikes = change.lookalikes ?? [];

  // The two pictures scroll together, so the same words always sit one above the other.
  const follow = (from: HTMLDivElement | null, to: HTMLDivElement | null) => {
    if (!from || !to || syncing.current) return;
    syncing.current = true;
    to.scrollLeft = from.scrollLeft;
    requestAnimationFrame(() => (syncing.current = false));
  };

  const shot = (label: string, version: number, ref: React.RefObject<HTMLDivElement | null>, other: React.RefObject<HTMLDivElement | null>, alt: string) => (
    <figure className="check-shot">
      <figcaption>{label}</figcaption>
      <div className="check-frame">
        <div className="check-strip" ref={ref} dir={dir} onScroll={() => follow(ref.current, other.current)}>
          <img src={areaImageUrl(doc, page, version, area, pixels)} width={width} height={height} alt={alt} draggable={false} />
        </div>
      </div>
    </figure>
  );

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

      {shot("Before", index, before, after, "The spot before the change, magnified")}
      {shot("After", index + 1, after, before, "The spot after the change, magnified")}

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
