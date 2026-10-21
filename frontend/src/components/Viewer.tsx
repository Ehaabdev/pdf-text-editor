import { downloadUrl, type DocInfo } from "../api";
import PageView from "./PageView";

function CloseIcon() {
  return (
    <svg viewBox="0 0 20 20" width="20" height="20" aria-hidden="true">
      <path d="M5 5l10 10M15 5L5 15" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" />
    </svg>
  );
}

function DownloadIcon() {
  return (
    <svg viewBox="0 0 20 20" width="20" height="20" aria-hidden="true">
      <path
        d="M10 3v9m0 0l-3.5-3.5M10 12l3.5-3.5M4 14.5V16h12v-1.5"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.8"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

export default function Viewer({ doc, onClose }: { doc: DocInfo; onClose: () => void }) {
  return (
    <div className="desk viewer">
      <header className="topbar">
        <button className="btn btn-ghost" onClick={onClose} aria-label="Close this PDF and open another">
          <CloseIcon />
          <span className="wide-only">Open another PDF</span>
        </button>
        <div className="doc-title">
          <span className="doc-name" title={doc.name}>
            {doc.name}
          </span>
          <span className="doc-pages">
            {doc.pageCount} {doc.pageCount === 1 ? "page" : "pages"}
          </span>
        </div>
        <a className="btn btn-primary" href={downloadUrl(doc)} download={doc.name}>
          <DownloadIcon />
          <span>
            Download<span className="wide-only"> PDF</span>
          </span>
        </a>
      </header>

      {doc.isSigned && (
        <p className="banner" role="status">
          This PDF has a digital signature. Editing it will make that signature invalid.
        </p>
      )}

      <main className="pages">
        {doc.pages.map((_, i) => (
          <PageView key={`${doc.revision}-${i}`} doc={doc} index={i} />
        ))}
      </main>
    </div>
  );
}
