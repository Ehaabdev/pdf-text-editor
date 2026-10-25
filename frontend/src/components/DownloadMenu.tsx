import { useEffect, useRef, useState } from "react";
import { ApiError, downloadUrl, exportDocument, type DocInfo, type ExportFormat } from "../api";
import { CHEVRON_DOWN, DOWNLOAD, Icon } from "./Icon";

type Item = { key: "pdf" | ExportFormat; name: string; note: string; badge: string; tone: string };

const ITEMS: Item[] = [
  { key: "pdf", name: "PDF", note: "Your edited file, as it is.", badge: "PDF", tone: "pdf" },
  { key: "pptx", name: "PowerPoint", note: "Each page a slide, with text you can edit.", badge: "PPT", tone: "pptx" },
  { key: "pptx-pictures", name: "PowerPoint, as pictures", note: "Looks exactly the same. The text can't be edited.", badge: "PPT", tone: "pptx" },
  { key: "xlsx", name: "Excel", note: "The tables, with numbers you can add up.", badge: "XLS", tone: "xlsx" },
  { key: "jpg", name: "JPG pictures", note: "A picture of every page.", badge: "JPG", tone: "img" },
  { key: "png", name: "PNG pictures", note: "Sharper text, larger files.", badge: "PNG", tone: "img" },
];

type Props = {
  doc: DocInfo;
  /** A file was downloaded: the changes are kept somewhere now. */
  onDownloaded: () => void;
  onError: (message: string) => void;
};

/** The Download button: the edited PDF, or the same pages as another kind of file. */
export default function DownloadMenu({ doc, onDownloaded, onError }: Props) {
  const [open, setOpen] = useState(false);
  const [making, setMaking] = useState<ExportFormat | null>(null);
  const wrap = useRef<HTMLSpanElement>(null);
  const menu = useRef<HTMLDivElement>(null);

  // Closes on a click elsewhere or Escape; the first choice takes the keyboard focus.
  useEffect(() => {
    if (!open) return;
    menu.current?.querySelector<HTMLElement>(".download-item")?.focus();
    const away = (e: PointerEvent) => {
      if (!wrap.current?.contains(e.target as Node)) setOpen(false);
    };
    const keys = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        setOpen(false);
        wrap.current?.querySelector<HTMLElement>(".btn-primary")?.focus();
        return;
      }
      if (e.key !== "ArrowDown" && e.key !== "ArrowUp") return;
      const items = [...(menu.current?.querySelectorAll<HTMLElement>(".download-item") ?? [])];
      const at = items.indexOf(document.activeElement as HTMLElement);
      const next = items[(at + (e.key === "ArrowDown" ? 1 : -1) + items.length) % items.length];
      if (next) {
        e.preventDefault();
        next.focus();
      }
    };
    document.addEventListener("pointerdown", away);
    document.addEventListener("keydown", keys);
    return () => {
      document.removeEventListener("pointerdown", away);
      document.removeEventListener("keydown", keys);
    };
  }, [open]);

  async function make(format: ExportFormat) {
    if (making) return;
    setMaking(format);
    try {
      const { blob, filename } = await exportDocument(doc, format);
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = filename;
      document.body.append(a);
      a.click();
      a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 60_000);
      onDownloaded();
      setOpen(false);
    } catch (e) {
      onError(e instanceof ApiError ? e.message : "Couldn't make the file. Try again.");
    } finally {
      setMaking(null);
    }
  }

  const badge = (item: Item) => (
    <span className={`download-badge download-badge--${item.tone}`} aria-hidden="true">
      {item.badge}
    </span>
  );
  const text = (item: Item) => (
    <span className="download-text">
      <span className="download-name">{item.name}</span>
      <span className="download-note">{making === item.key ? "Making the file…" : item.note}</span>
    </span>
  );

  return (
    <span className="download-wrap" ref={wrap}>
      <button className="btn btn-primary" aria-haspopup="menu" aria-expanded={open} aria-label="Download" onClick={() => setOpen((v) => !v)}>
        <Icon d={DOWNLOAD} />
        <span className="wide-only">Download</span>
        <span className="wide-only download-chevron">
          <Icon d={CHEVRON_DOWN} size={14} />
        </span>
      </button>
      {open && (
        <div className="download-menu" role="menu" aria-label="Download as" ref={menu}>
          <p className="download-title">Download as</p>
          {ITEMS.map((item) =>
            item.key === "pdf" ? (
              <a
                key={item.key}
                role="menuitem"
                className="download-item"
                href={downloadUrl(doc)}
                download={doc.name}
                onClick={() => {
                  onDownloaded();
                  setOpen(false);
                }}
              >
                {badge(item)}
                {text(item)}
              </a>
            ) : (
              <button
                key={item.key}
                role="menuitem"
                className="download-item"
                disabled={making !== null && making !== item.key}
                aria-busy={making === item.key}
                onClick={() => void make(item.key as ExportFormat)}
              >
                {badge(item)}
                {text(item)}
                {making === item.key && <span className="spinner" aria-hidden="true" />}
              </button>
            ),
          )}
        </div>
      )}
    </span>
  );
}
