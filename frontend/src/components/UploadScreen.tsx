import { useEffect, useRef, useState } from "react";
import { uploadPdf, type DocInfo } from "../api";
import { APP_NAME, SOURCE_URL } from "../config";
import Demo from "./Demo";
import { Icon, Mark, UPLOAD } from "./Icon";

const MAX_MB = 20;

type Status =
  | { kind: "idle" }
  | { kind: "uploading"; progress: number }
  | { kind: "opening" }
  | { kind: "error"; message: string };

function looksLikePdf(file: File) {
  return file.type === "application/pdf" || file.name.toLowerCase().endsWith(".pdf");
}

export default function UploadScreen({ onOpen, notice }: { onOpen: (d: DocInfo) => void; notice: string | null }) {
  const input = useRef<HTMLInputElement>(null);
  const [status, setStatus] = useState<Status>({ kind: "idle" });
  const [dragging, setDragging] = useState(false);
  const busy = status.kind === "uploading" || status.kind === "opening";

  async function start(file: File) {
    if (busy) return;
    if (!looksLikePdf(file)) {
      setStatus({ kind: "error", message: `“${file.name}” isn't a PDF. Choose a file that ends in .pdf.` });
      return;
    }
    if (file.size > MAX_MB * 1024 * 1024) {
      setStatus({ kind: "error", message: `This file is larger than ${MAX_MB} MB. Choose a smaller PDF.` });
      return;
    }
    setStatus({ kind: "uploading", progress: 0 });
    try {
      const doc = await uploadPdf(file, (p) =>
        setStatus(p < 1 ? { kind: "uploading", progress: p } : { kind: "opening" }),
      );
      onOpen(doc);
    } catch (e) {
      setStatus({ kind: "error", message: e instanceof Error ? e.message : String(e) });
    }
  }

  // Accept a file dropped anywhere on the page, not only on the sheet.
  useEffect(() => {
    let depth = 0;
    const hasFiles = (e: DragEvent) => e.dataTransfer?.types.includes("Files") ?? false;
    const enter = (e: DragEvent) => {
      if (!hasFiles(e)) return;
      depth++;
      setDragging(true);
    };
    const leave = (e: DragEvent) => {
      if (!hasFiles(e)) return;
      depth = Math.max(0, depth - 1);
      if (depth === 0) setDragging(false);
    };
    const over = (e: DragEvent) => {
      if (hasFiles(e)) e.preventDefault();
    };
    const drop = (e: DragEvent) => {
      if (!hasFiles(e)) return;
      e.preventDefault();
      depth = 0;
      setDragging(false);
      const file = e.dataTransfer?.files[0];
      if (file) void start(file);
    };
    window.addEventListener("dragenter", enter);
    window.addEventListener("dragleave", leave);
    window.addEventListener("dragover", over);
    window.addEventListener("drop", drop);
    return () => {
      window.removeEventListener("dragenter", enter);
      window.removeEventListener("dragleave", leave);
      window.removeEventListener("dragover", over);
      window.removeEventListener("drop", drop);
    };
  });

  return (
    <div className={`landing${dragging ? " landing--dragging" : ""}`}>
      <header className="landing-bar glass">
        <span className="brand">
          <Mark size={28} />
          {APP_NAME}
        </span>
        <a className="quiet-link" href={SOURCE_URL} target="_blank" rel="noreferrer">
          Source code
        </a>
      </header>

      <main className="landing-main">
        <section className="hero" aria-labelledby="upload-title">
          <div className="hero-copy">
            <h1 id="upload-title" className="hero-title">
              Edit the text in{" "}
              <span className="selection">
                your PDF
                <span className="caret" aria-hidden="true" />
              </span>
            </h1>
            <p className="hero-lede">
              Change, add or remove words. Your edits keep the original font, size and color, and each change is
              checked so nothing else on the page moves.
            </p>

            <div className="dropzone glass">
              <button className="btn btn-primary btn-large" onClick={() => input.current?.click()} disabled={busy}>
                <Icon d={UPLOAD} size={20} />
                {busy ? "Opening your PDF…" : "Choose a PDF"}
              </button>
              <span className="hint">{dragging ? "Drop it to open" : "or drop it anywhere on this page"}</span>
              <input
                ref={input}
                type="file"
                accept="application/pdf,.pdf"
                hidden
                onChange={(e) => {
                  const file = e.target.files?.[0];
                  e.target.value = "";
                  if (file) void start(file);
                }}
              />

              <div className="upload-status" aria-live="polite">
                {status.kind === "uploading" && (
                  <div className="progress">
                    <div className="progress-bar" style={{ width: `${Math.round(status.progress * 100)}%` }} />
                    <span className="progress-label">Uploading… {Math.round(status.progress * 100)}%</span>
                  </div>
                )}
                {status.kind === "opening" && (
                  <div className="progress">
                    <div className="progress-bar progress-bar--full" />
                    <span className="progress-label">Reading the pages…</span>
                  </div>
                )}
                {status.kind === "error" && (
                  <p className="error" role="alert">
                    {status.message}
                  </p>
                )}
                {status.kind === "idle" && notice && <p className="notice-text">{notice}</p>}
              </div>
            </div>

            <ul className="facts">
              <li>
                English, Français,{" "}
                <span lang="ar" dir="rtl">
                  العربية
                </span>
              </li>
              <li>Free, no account</li>
              <li>Deleted an hour after you last use it</li>
            </ul>
          </div>

          <Demo />
        </section>
      </main>
    </div>
  );
}
