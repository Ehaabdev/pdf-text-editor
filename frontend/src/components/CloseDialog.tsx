import { useEffect, useRef } from "react";
import { downloadUrl, type DocInfo } from "../api";
import { DOWNLOAD, Icon } from "./Icon";

type Props = {
  doc: DocInfo;
  onDownload: () => void;
  onClose: () => void;
  onCancel: () => void;
};

/** Closing deletes the file from the server: asked first when its changes weren't downloaded. */
export default function CloseDialog({ doc, onDownload, onClose, onCancel }: Props) {
  const dialog = useRef<HTMLDialogElement>(null);
  const stay = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    const d = dialog.current;
    if (d && !d.open) d.showModal();
    // The safe choice has the focus, so Enter keeps the work.
    stay.current?.focus();
  }, []);

  const applied = doc.history.filter((c) => c.applied).length;
  const changes = applied === 1 ? "your change" : applied > 1 ? `your ${applied} changes` : "your changes";
  const them = applied === 1 ? "it" : "them";

  return (
    <dialog
      ref={dialog}
      className="dialog"
      aria-labelledby="close-title"
      aria-describedby="close-text"
      onCancel={(e) => {
        // Escape: keep editing.
        e.preventDefault();
        onCancel();
      }}
    >
      <h2 id="close-title" className="dialog-title">
        Close without downloading?
      </h2>
      <p id="close-text" className="dialog-text">
        Closing deletes this file and {changes} from the server. Download the PDF first to keep {them}.
      </p>
      <div className="dialog-actions">
        <a className="btn btn-primary" href={downloadUrl(doc)} download={doc.name} onClick={onDownload}>
          <Icon d={DOWNLOAD} />
          Download PDF
        </a>
        <button className="btn btn-delete" onClick={onClose}>
          Close without downloading
        </button>
        <button ref={stay} className="btn btn-ghost" onClick={onCancel}>
          Keep editing
        </button>
      </div>
    </dialog>
  );
}
