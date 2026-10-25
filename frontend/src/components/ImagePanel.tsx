import { useEffect, useRef, useState } from "react";
import { ApiError, pictureUrl, replaceImage, type DocInfo, type EditOutcome, type PageImage } from "../api";
import { CLOSE, Icon } from "./Icon";

/** Pictures people can add or put in: what the server reads. */
export const PICTURE_TYPES = "image/png,image/jpeg,image/webp";
const MAX_MB = 15;

/** Is this a picture the server can use? Said before anything is sent. */
export function pictureProblem(file: File): string | null {
  if (!PICTURE_TYPES.split(",").includes(file.type)) return `“${file.name}” isn't a picture this editor can use. Use a JPEG or PNG.`;
  if (file.size > MAX_MB * 1024 * 1024) return `This picture is larger than ${MAX_MB} MB. Use a smaller one.`;
  return null;
}

/** A size in points, as people measure paper. */
export function cm(points: number) {
  return `${((points * 2.54) / 72).toFixed(1)} cm`;
}

type Fit = "fit" | "fill" | "stretch";
const FITS: { key: Fit; name: string; note: string; css: "contain" | "cover" | "fill" }[] = [
  { key: "fit", name: "Fit", note: "All of the new picture shows, inside the place.", css: "contain" },
  { key: "fill", name: "Fill", note: "Covers the whole place; edges may be cut off.", css: "cover" },
  { key: "stretch", name: "Stretch", note: "Takes the place's shape exactly, even if squeezed.", css: "fill" },
];

type Props = {
  doc: DocInfo;
  page: number;
  image: PageImage;
  deleting: boolean;
  onClose: () => void;
  onReplaced: (outcome: EditOutcome, everywhere: number) => void;
  onMove: () => void;
  onDelete: () => void;
  /** The server is checking this panel's change. */
  onWorking?: (working: boolean) => void;
};

/** A picked picture: replace it, move or resize it, or delete it. */
export default function ImagePanel({ doc, page, image, deleting, onClose, onReplaced, onMove, onDelete, onWorking }: Props) {
  const chooser = useRef<HTMLInputElement>(null);
  const [replacement, setReplacement] = useState<{ file: File; url: string } | null>(null);
  const [fit, setFit] = useState<Fit>("fit");
  const [everywhere, setEverywhere] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    onWorking?.(saving);
  }, [saving, onWorking]);
  useEffect(() => () => onWorking?.(false), [onWorking]);

  // Another picture was picked: start over. The chosen file's preview is let go.
  useEffect(() => {
    setReplacement(null);
    setError(null);
  }, [image.id, page]);
  useEffect(() => () => void (replacement && URL.revokeObjectURL(replacement.url)), [replacement]);

  function chose(file: File | undefined) {
    if (!file) return;
    const problem = pictureProblem(file);
    if (problem) {
      setError(problem);
      return;
    }
    setError(null);
    setReplacement({ file, url: URL.createObjectURL(file) });
  }

  async function replace() {
    if (!replacement || saving) return;
    setSaving(true);
    setError(null);
    try {
      const all = everywhere && image.pages > 1;
      onReplaced(await replaceImage(doc, page, image, replacement.file, fit, all), all ? image.pages : 1);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Something went wrong. Try again.");
    } finally {
      setSaving(false);
    }
  }

  const [x0, y0, x1, y1] = image.bbox;
  const what = image.kind === "drawing" ? "Drawing" : "Picture";
  const fitting = FITS.find((f) => f.key === fit)!;

  return (
    <section className="panel" aria-label={replacement ? "Replace picture" : `${what} on page ${page + 1}`}>
      <div className="panel-head">
        <h2 className="panel-title">{replacement ? "Replace picture" : `${what} on page ${page + 1}`}</h2>
        <button className="btn btn-ghost btn-icon" onClick={onClose} aria-label="Close">
          <Icon d={CLOSE} />
        </button>
      </div>

      <input
        ref={chooser}
        type="file"
        accept={PICTURE_TYPES}
        hidden
        onChange={(e) => {
          chose(e.target.files?.[0]);
          e.target.value = ""; // the same file can be chosen again
        }}
      />

      {replacement ? (
        <>
          {/* The new picture in the old one's place, as it will be put there. */}
          <figure
            className="picture-place picture-place--slot"
            style={(x1 - x0) / (y1 - y0) >= 1.5 ? { width: "100%", aspectRatio: `${x1 - x0} / ${y1 - y0}` } : { height: 160, aspectRatio: `${x1 - x0} / ${y1 - y0}` }}
          >
            <img src={replacement.url} alt="The new picture, in the old one's place" style={{ objectFit: fitting.css }} />
          </figure>
          <fieldset className="style-choice" disabled={saving}>
            <legend>How it fits the old picture's place</legend>
            {FITS.map((f) => (
              <label key={f.key} className={`style-option${fit === f.key ? " style-option--on" : ""}`}>
                <input type="radio" name="fit" checked={fit === f.key} onChange={() => setFit(f.key)} />
                <span className="style-option-text">
                  <span>{f.name}</span>
                  <span className="style-option-sample fit-note">{f.note}</span>
                </span>
              </label>
            ))}
          </fieldset>
          {image.pages > 1 && (
            <label className={`style-option item-option${everywhere ? " style-option--on" : ""}`}>
              <input type="checkbox" checked={everywhere} onChange={(e) => setEverywhere(e.target.checked)} disabled={saving} />
              On all {image.pages} pages it's on
            </label>
          )}
          {error && (
            <p className="error" role="alert">
              {error}
            </p>
          )}
          <div className="edit-actions">
            <button className="btn btn-primary" onClick={() => void replace()} disabled={saving}>
              {saving && <span className="spinner" aria-hidden="true" />}
              {saving ? "Checking the change…" : "Replace picture"}
            </button>
            <button className="btn btn-secondary" onClick={() => chooser.current?.click()} disabled={saving}>
              Choose another…
            </button>
            <button className="btn btn-ghost" onClick={() => setReplacement(null)} disabled={saving}>
              Cancel
            </button>
          </div>
        </>
      ) : (
        <>
          <figure className="picture-place">
            <img src={pictureUrl(doc, page, image, 800)} alt={`The ${what.toLowerCase()} you picked`} draggable={false} />
          </figure>
          <p className="panel-note">
            {cm(x1 - x0)} wide, {cm(y1 - y0)} high
            {image.kind === "image" && image.width > 0 ? ` (${image.width} × ${image.height} pixels)` : ""}.
            {image.pages > 1 ? ` The same ${what.toLowerCase()} is on ${image.pages} pages.` : ""}
          </p>
          {error && (
            <p className="error" role="alert">
              {error}
            </p>
          )}
          <div className="edit-actions">
            <button className="btn btn-primary" onClick={() => chooser.current?.click()} disabled={deleting}>
              Replace…
            </button>
            <button className="btn btn-secondary" onClick={onMove} disabled={deleting}>
              Move or resize
            </button>
            <button className="btn btn-delete" onClick={onDelete} disabled={deleting}>
              {deleting && <span className="spinner" aria-hidden="true" />}
              {deleting ? "Deleting…" : "Delete picture"}
            </button>
          </div>
        </>
      )}
    </section>
  );
}

type PlaceProps = {
  mode: "add" | "move";
  box: number[];
  saving: boolean;
  error: string | null;
  onSave: () => void;
  onChooseAnother?: () => void;
  onCancel: () => void;
};

/** A picture being put on the page, or moved: it's dragged on the page; this saves it. */
export function PlacePicturePanel({ mode, box, saving, error, onSave, onChooseAnother, onCancel }: PlaceProps) {
  const title = mode === "add" ? "Add picture" : "Move or resize";
  return (
    <section className="panel" aria-label={title}>
      <div className="panel-head">
        <h2 className="panel-title">{title}</h2>
        <button className="btn btn-ghost btn-icon" onClick={onCancel} aria-label="Cancel" disabled={saving}>
          <Icon d={CLOSE} />
        </button>
      </div>
      <p className="panel-lede">
        Drag the picture where you want it, or drag a corner to change its size.
        {mode === "add" ? (
          <>
            {" "}
            <span className="wide-only">Clicking</span>
            <span className="narrow-only">Tapping</span> another spot, on any page, puts it there.
          </>
        ) : (
          " A dashed line shows where it was."
        )}
      </p>
      <p className="panel-note">
        {cm(box[2] - box[0])} wide, {cm(box[3] - box[1])} high.
      </p>
      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}
      <div className="edit-actions">
        <button className="btn btn-primary" onClick={onSave} disabled={saving}>
          {saving && <span className="spinner" aria-hidden="true" />}
          {saving ? "Checking the change…" : mode === "add" ? "Add picture" : "Save change"}
        </button>
        {onChooseAnother && (
          <button className="btn btn-secondary" onClick={onChooseAnother} disabled={saving}>
            Choose another…
          </button>
        )}
        <button className="btn btn-ghost" onClick={onCancel} disabled={saving}>
          Cancel
        </button>
      </div>
    </section>
  );
}
