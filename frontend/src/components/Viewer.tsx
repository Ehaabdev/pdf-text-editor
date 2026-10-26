import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import {
  ApiError,
  addImage,
  deleteImage,
  deleteLines,
  getLines,
  goToVersion,
  moveImage,
  moveLines,
  pictureUrl,
  previewMove,
  redoEdit,
  undoEdit,
  type DocInfo,
  type EditOutcome,
  type PageImage,
  type PickedLine,
  type SpotPlan,
  type TextLine,
} from "../api";
import AddTextPanel, { type Spot } from "./AddTextPanel";
import ChangesPanel from "./ChangesPanel";
import CheckPanel from "./CheckPanel";
import CloseDialog from "./CloseDialog";
import DownloadMenu from "./DownloadMenu";
import HomePanel from "./HomePanel";
import { ADD_TEXT, BOXES, CHANGES, CLOSE, HELP, Icon, MAGNIFIER, Mark, MINUS, PAGES, PICTURE, PLUS, REDO, UNDO } from "./Icon";
import ImagePanel, { PICTURE_TYPES, PlacePicturePanel, pictureProblem } from "./ImagePanel";
import LinePanel, { type Change } from "./LinePanel";
import { MovePanel, TogetherPanel } from "./MovePanel";
import PageView, { type Flash } from "./PageView";
import SelectionPanel from "./SelectionPanel";
import PagesPanel from "./PagesPanel";
import ThemeSwitch from "./ThemeSwitch";
import Thumbnails from "./Thumbnails";
import Tour, { editorSteps, type Step } from "./Tour";

// On wide screens an A4 page (595pt) shows about 860px wide at 100%.
const CSS_PX_PER_PT = 1.45;
// Space beside the pages: matches .pages-inner's padding.
const gutter = (width: number) => (width < 640 ? 12 : 24);
const ZOOMS = [1, 1.5, 2, 3, 4];

function useWidth(ref: React.RefObject<HTMLElement | null>) {
  const [width, setWidth] = useState(0);
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    const ro = new ResizeObserver(() => setWidth(el.clientWidth));
    ro.observe(el);
    setWidth(el.clientWidth);
    return () => ro.disconnect();
  }, [ref]);
  return width;
}

type Notice = { text: string; tone: "done" | "warning" | "error"; detail?: string };
type HistoryEntry = DocInfo["history"][number];
type Box = [number, number, number, number];
/** A picture being put on a page (a new one) or moved, or lines or a table being moved
 *  ("text": shown as a picture of them alone): dragged in a frame until saved. */
type Frame = {
  page: number;
  box: Box;
  mode: "add" | "move" | "text";
  src: string;
  from?: Box;
  image?: PageImage;
  file?: File;
  lines?: TextLine[];
  table?: boolean;
};
/** Lines the PDF stores as one with others: asked before moving them all. */
type Together = { page: number; lines: TextLine[]; table: boolean; others: string[]; message: string };

// Every change the server accepts has passed its checks, among them that nothing outside
// the changed lines differs by a single pixel.
const CHECKED = "Checked: nothing else on the page changed.";
// When text moved to make room (or into a deleted line's space), it was also checked to
// look exactly the same at its new place.
const CHECKED_MOVED = "Checked: the text that moved looks exactly the same, and nothing else changed.";

// The Help button's "New here?" bubble shows until the tour is opened or a line is picked.
const HELP_SEEN = "pdfedit.helpSeen";
function helpSeen() {
  try {
    return localStorage.getItem(HELP_SEEN) === "1";
  } catch {
    return true;
  }
}
function markHelpSeen() {
  try {
    localStorage.setItem(HELP_SEEN, "1");
  } catch {
    // Storage can be unavailable (private mode): the bubble shows again next time.
  }
}

export default function Viewer({ doc, onClose, onChange }: { doc: DocInfo; onClose: () => void; onChange: (d: DocInfo) => void }) {
  const [notice, setNotice] = useState<Notice | null>(null);
  const [busy, setBusy] = useState(false);
  const [showChanges, setShowChanges] = useState(false);
  // Comparing with the original: which version the pages show, with the changes outlined.
  const [compare, setCompare] = useState<"original" | "now" | null>(null);
  // A close look at one change (doc.history index), opened after a change that used a
  // look-alike font, or from the list of changes.
  const [checking, setChecking] = useState<number | null>(null);

  // Confirmations fade after a while; warnings and errors stay until dismissed.
  useEffect(() => {
    if (notice?.tone !== "done") return;
    const t = setTimeout(() => setNotice(null), 5000);
    return () => clearTimeout(t);
  }, [notice]);

  // Where the last change was made: lit up on the page once its new image is showing.
  const [flash, setFlash] = useState<Flash | null>(null);
  useEffect(() => {
    if (!flash) return;
    const t = setTimeout(() => setFlash(null), 4000);
    return () => clearTimeout(t);
  }, [flash]);

  function lightUp(d: DocInfo, change: HistoryEntry | undefined) {
    const rects = change ? (change.marks ?? change.areas ?? []) : [];
    setFlash(rects.length ? { revision: d.revision, rects } : null);
  }

  // A panel is waiting for the server: the selected lines shimmer meanwhile.
  const [working, setWorking] = useState(false);

  // Nothing left to compare: back to editing.
  useEffect(() => {
    if (doc.version === 0) setCompare(null);
  }, [doc.version]);

  function report(what: string, outcome: EditOutcome) {
    // A look-alike font is explained in the check panel that opens: not twice.
    const subs = (outcome.lookalikes ?? []).map((l) => l.substitute);
    const warnings = outcome.warnings.filter((w) => !subs.some((s) => w.includes(s)));
    setNotice(
      warnings.length
        ? { text: `${what} ${warnings.join(" ")}`, tone: "warning" }
        : { text: what, tone: "done", detail: outcome.moved ? CHECKED_MOVED : CHECKED },
    );
  }

  /** After a change that used a look-alike font: ask the user to check it. */
  function offerCheck(outcome: EditOutcome) {
    if (outcome.lookalikes?.length) setChecking(outcome.document.history.length - 1);
  }

  function edited(outcome: EditOutcome, change: Change) {
    clearSelection();
    // Saved as it already was: the server made no new version, so there's nothing to light up.
    if (outcome.document.revision === doc.revision) {
      const text = "Nothing changed: the text is the same as before.";
      setNotice(outcome.warnings.length ? { text: `${text} ${outcome.warnings.join(" ")}`, tone: "warning" } : { text, tone: "done" });
      return;
    }
    onChange(outcome.document);
    lightUp(outcome.document, outcome.document.history.at(-1));
    offerCheck(outcome);
    const moved = (outcome.moved ?? 0) > 0;
    report(
      change === "row added"
        ? "Row added. What was below it moved down to make room."
        : change === "row deleted"
          ? "Row deleted. What was below it moved up."
          : change === "column deleted"
            ? "Column deleted. The table is narrower now."
          : change === "deleted"
            ? moved ? "Line deleted. The text below moved up." : "Line deleted."
            : change === "added"
              ? moved ? "Line added. The text below moved down to make room." : "Line added."
              : "Line changed.",
      outcome,
    );
  }

  // Closing deletes the file from the server. With changes not yet downloaded (in this
  // version), the user is asked first, and the browser warns before the tab closes.
  const [downloaded, setDownloaded] = useState<number | null>(null);
  const [askClose, setAskClose] = useState(false);
  const unsaved = doc.history.length > 0 && downloaded !== doc.revision;
  useEffect(() => {
    if (!unsaved) return;
    const warn = (e: BeforeUnloadEvent) => {
      e.preventDefault();
      e.returnValue = ""; // older browsers need this too
    };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [unsaved]);

  const [deleting, setDeleting] = useState(false);
  async function remove(lines: PickedLine[]) {
    if (!lines.length || deleting) return;
    setDeleting(true);
    try {
      const outcome = await deleteLines(doc, lines);
      clearSelection();
      onChange(outcome.document);
      lightUp(outcome.document, outcome.document.history.at(-1));
      const moved = (outcome.moved ?? 0) > 0 ? " The text below moved up." : "";
      report(lines.length === 1 ? `Line deleted.${moved}` : `${lines.length} lines deleted.${moved}`, outcome);
    } catch (e) {
      setNotice({ text: e instanceof ApiError ? e.message : "Couldn't delete. Try again.", tone: "error" });
    } finally {
      setDeleting(false);
    }
  }

  /** Undo, redo, or jump to a version: the pages change, nothing else does. */
  async function move(action: () => Promise<DocInfo>, done: string, failed: string, light?: (d: DocInfo) => HistoryEntry | undefined) {
    setBusy(true);
    try {
      const d = await action();
      onChange(d);
      if (light) lightUp(d, light(d));
      setChecking(null);
      clearSelection();
      stopPlacing();
      cancelFrame();
      setNotice({ text: done, tone: "done" });
    } catch (e) {
      setNotice({ text: e instanceof ApiError ? e.message : failed, tone: "error" });
    } finally {
      setBusy(false);
    }
  }
  // Undo lights up the change it took back; redo the one it put back.
  const undo = () => move(() => undoEdit(doc), "Last change undone.", "Couldn't undo. Try again.", (d) => d.history[d.version]);
  const redo = () => move(() => redoEdit(doc), "Change redone.", "Couldn't redo. Try again.", (d) => d.history[d.version - 1]);
  const goTo = (version: number) =>
    move(
      () => goToVersion(doc, version),
      version === 0 ? "Back to the original. Your changes can still be redone." : "Changes redone.",
      "Couldn't do that. Try again.",
    );

  const scroller = useRef<HTMLElement>(null);
  const available = useWidth(scroller);
  const [zoomStep, setZoomStep] = useState(0);
  const [showText, setShowText] = useState(false);
  // A lens over the page that follows the mouse (wide screens).
  const [magnifier, setMagnifier] = useState(false);
  // Selected lines, in the order they were picked. In "adding" mode (for touch screens)
  // every tap adds or removes a line; with a mouse, Shift or Ctrl does the same.
  const [picked, setPicked] = useState<PickedLine[]>([]);
  const [adding, setAdding] = useState(false);
  // Adding text: tap a spot on a page, then type.
  const [placing, setPlacing] = useState(false);
  const [spot, setSpot] = useState<Spot | null>(null);
  // What adding at the spot will do (a new item, a new line, joining a line, or there):
  // the page shows where.
  const [spotPlan, setSpotPlan] = useState<SpotPlan | null>(null);
  // A picked picture, and a picture being added or moved: dragged in a frame until saved.
  const [imagePick, setImagePick] = useState<{ page: number; image: PageImage } | null>(null);
  const [frame, setFrame] = useState<Frame | null>(null);
  const [frameSaving, setFrameSaving] = useState(false);
  const [frameError, setFrameError] = useState<string | null>(null);
  const [together, setTogether] = useState<Together | null>(null);
  const [moveBusy, setMoveBusy] = useState(false);
  const pictureInput = useRef<HTMLInputElement>(null);
  // A new picture is shown from the file itself until it's added: let go of it after.
  const addUrl = frame?.mode === "add" ? frame.src : null;
  useEffect(
    () => () => {
      if (addUrl) URL.revokeObjectURL(addUrl);
    },
    [addUrl],
  );
  // Pages: the list beside the pages (wide screens), or a panel of them (from the page chip).
  const [showPages, setShowPages] = useState(false);
  const [current, setCurrent] = useState(0);
  // The guided tour (Help), and the first-time bubble pointing at it.
  const [tour, setTour] = useState<Step[] | null>(null);
  const [hint, setHint] = useState(() => !helpSeen());

  function stopPlacing() {
    setPlacing(false);
    setSpot(null);
    setSpotPlan(null);
  }
  const zoom = ZOOMS[zoomStep];

  function clearSelection() {
    setPicked([]);
    setAdding(false);
    setImagePick(null);
  }

  function cancelFrame() {
    setFrame(null);
    setFrameError(null);
  }

  /** The spot in the middle of the view, on a page (points, as displayed). */
  function viewCenter(page: number): [number, number] {
    const size = doc.pages[page];
    const el = scroller.current;
    const box = document.querySelector(`#page-${page} .page`);
    if (!el || !box) return [size.width / 2, size.height / 2];
    const a = el.getBoundingClientRect();
    const r = box.getBoundingClientRect();
    const x = (((a.left + a.right) / 2 - r.left) / r.width) * size.width;
    const y = (((a.top + a.bottom) / 2 - r.top) / r.height) * size.height;
    return [Math.min(Math.max(x, 0), size.width), Math.min(Math.max(y, 0), size.height)];
  }

  /** A w x h box centered on a spot, kept on the page (made smaller if it can't fit). */
  function around(page: number, cx: number, cy: number, w: number, h: number): Box {
    const size = doc.pages[page];
    const s = Math.min(1, size.width / w, size.height / h);
    w *= s;
    h *= s;
    const x0 = Math.min(Math.max(cx - w / 2, 0), size.width - w);
    const y0 = Math.min(Math.max(cy - h / 2, 0), size.height - h);
    return [x0, y0, x0 + w, y0 + h];
  }

  function selectImage(page: number, image: PageImage) {
    setPicked([]);
    setAdding(false);
    setShowPages(false);
    setImagePick((now) => (now?.page === page && now.image.id === image.id ? null : { page, image }));
  }

  /** A new picture was chosen: it shows in the middle of the view, to drag into place. */
  function pictureChosen(file: File | undefined) {
    if (!file) return;
    const problem = pictureProblem(file);
    if (problem) {
      setNotice({ text: problem, tone: "error" });
      return;
    }
    const url = URL.createObjectURL(file);
    const img = new Image();
    img.onload = () => {
      // Choosing another picture keeps the place; otherwise the page in view.
      const page = frame?.mode === "add" ? frame.page : current;
      const size = doc.pages[page];
      // As large as it would print at 96 dots per inch, but at most a third of the page.
      let w = Math.max(24, Math.min(size.width * 0.3, img.naturalWidth * 0.75));
      let h = (w * img.naturalHeight) / img.naturalWidth;
      if (h > size.height * 0.4) {
        w *= (size.height * 0.4) / h;
        h = size.height * 0.4;
      }
      const [cx, cy] = frame?.mode === "add" ? [(frame.box[0] + frame.box[2]) / 2, (frame.box[1] + frame.box[3]) / 2] : viewCenter(page);
      clearSelection();
      stopPlacing();
      setShowChanges(false);
      setShowPages(false);
      setFrameError(null);
      setFrame({ page, box: around(page, cx, cy, w, h), mode: "add", src: url, file });
    };
    img.onerror = () => {
      URL.revokeObjectURL(url);
      setNotice({ text: `“${file.name}” couldn't be opened as a picture. Use a JPEG or PNG.`, tone: "error" });
    };
    img.src = url;
  }

  /** Lines on a page (with `table`, the table the first is in) picked up to be dragged:
   *  shown as a picture of them alone, over their place. */
  async function startTextMove(page: number, lines: TextLine[], table: boolean) {
    if (moveBusy) return;
    setMoveBusy(true);
    setFrameError(null);
    try {
      const plan = await previewMove(doc, page, lines, table);
      if (plan.together) {
        setTogether({ page, lines, table, others: plan.together, message: plan.message });
        return;
      }
      setTogether(null);
      clearSelection();
      stopPlacing();
      setShowChanges(false);
      setShowPages(false);
      setFrame({ page, box: [...plan.box], mode: "text", src: plan.image, from: [...plan.box], lines, table });
    } catch (e) {
      setNotice({ text: e instanceof ApiError ? e.message : "Something went wrong. Try again.", tone: "error" });
    } finally {
      setMoveBusy(false);
    }
  }

  /** Move the lines with the ones the PDF stores them with. */
  async function moveTogether() {
    if (!together) return;
    const { page, lines, table, others } = together;
    const all = (await getLines(doc, page)).lines;
    const more = all.filter((l) => others.includes(l.id) && !lines.some((m) => m.id === l.id));
    await startTextMove(page, [...lines, ...more], table);
  }

  function startMove() {
    if (!imagePick) return;
    const { page, image } = imagePick;
    setFrameError(null);
    setFrame({ page, box: [...image.bbox], mode: "move", src: pictureUrl(doc, page, image, 800), from: [...image.bbox], image });
  }

  /** A tap on a page while something is in a frame: it goes there. A new picture can go
   *  on any page; a picture, lines or a table being moved stay on their page. */
  function placeFrame(page: number, x: number, y: number) {
    if (!frame || (frame.mode !== "add" && page !== frame.page)) return;
    setFrame({ ...frame, page, box: around(page, x, y, frame.box[2] - frame.box[0], frame.box[3] - frame.box[1]) });
  }

  function pictureChanged(outcome: EditOutcome, what: string) {
    setImagePick(null);
    onChange(outcome.document);
    lightUp(outcome.document, outcome.document.history.at(-1));
    report(what, outcome);
  }

  async function saveFrame() {
    if (!frame || frameSaving) return;
    const f = frame;
    const from = f.from;
    if (from && f.box.every((v, k) => Math.abs(v - from[k]) < 0.25)) {
      cancelFrame(); // not moved: nothing to save
      return;
    }
    const resized = from && (Math.abs(f.box[2] - f.box[0] - (from[2] - from[0])) > 0.5 || Math.abs(f.box[3] - f.box[1] - (from[3] - from[1])) > 0.5);
    setFrameSaving(true);
    setFrameError(null);
    if (f.mode === "text" && from) {
      try {
        const outcome = await moveLines(doc, f.page, f.lines ?? [], !!f.table, f.box[0] - from[0], f.box[1] - from[1]);
        setFrame(null);
        onChange(outcome.document);
        lightUp(outcome.document, outcome.document.history.at(-1));
        const n = f.lines?.length ?? 0;
        report(f.table ? "Table moved." : n === 1 ? "Line moved." : "Lines moved.", outcome);
      } catch (e) {
        setFrameError(e instanceof ApiError ? e.message : "Something went wrong. Try again.");
      } finally {
        setFrameSaving(false);
      }
      return;
    }
    try {
      const outcome = f.mode === "add" ? await addImage(doc, f.page, f.file!, f.box) : await moveImage(doc, f.page, f.image!, f.box);
      setFrame(null);
      pictureChanged(outcome, f.mode === "add" ? "Picture added." : resized ? "Picture resized." : "Picture moved.");
    } catch (e) {
      setFrameError(e instanceof ApiError ? e.message : "Something went wrong. Try again.");
    } finally {
      setFrameSaving(false);
    }
  }

  async function removePicture() {
    if (!imagePick || deleting) return;
    setDeleting(true);
    try {
      pictureChanged(await deleteImage(doc, imagePick.page, imagePick.image), "Picture deleted.");
    } catch (e) {
      setNotice({ text: e instanceof ApiError ? e.message : "Couldn't delete. Try again.", tone: "error" });
    } finally {
      setDeleting(false);
    }
  }

  // On phones the panel opens under the pages and takes part of the screen. The line being
  // worked on (or the add-text mark) is scrolled back into view above it when it's picked,
  // and whenever the pages' area gets shorter: the panel opens or grows, the keyboard shows.
  // The same goes for a picked picture, and a picture being placed.
  const focus = useRef<string>(".spot");
  const f = picked.at(-1);
  focus.current = frame
    ? `#page-${frame.page} .image-frame`
    : imagePick
      ? `#page-${imagePick.page} [data-image="${CSS.escape(imagePick.image.id)}"]`
      : f
        ? `#page-${f.page} [data-line="${CSS.escape(f.line.id)}"]`
        : ".spot";
  const keepInView = useCallback(() => {
    const el = scroller.current;
    if (!el) return;
    const target = el.querySelector<HTMLElement>(focus.current);
    if (!target) return;
    const box = el.getBoundingClientRect();
    const r = target.getBoundingClientRect();
    if (r.top >= box.top + 12 && r.bottom <= box.bottom - 12) return;
    const still = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
    el.scrollBy({ top: r.top - box.top - el.clientHeight / 3, behavior: still ? "auto" : "smooth" });
  }, []);

  const lastKey = f ? `${f.page}:${f.line.id}` : imagePick ? `${imagePick.page}:${imagePick.image.id}` : "";
  const frameKey = frame ? `${frame.page}:${frame.mode}` : "";
  useEffect(() => {
    const id = requestAnimationFrame(keepInView);
    return () => cancelAnimationFrame(id);
  }, [lastKey, frameKey, spot, keepInView]);

  useEffect(() => {
    const el = scroller.current;
    if (!el) return;
    let height = el.clientHeight;
    const ro = new ResizeObserver(() => {
      if (el.clientHeight < height) keepInView();
      height = el.clientHeight;
    });
    ro.observe(el);
    window.visualViewport?.addEventListener("resize", keepInView);
    return () => {
      ro.disconnect();
      window.visualViewport?.removeEventListener("resize", keepInView);
    };
  }, [keepInView]);

  function startComparing(show: "original" | "now" = "original") {
    clearSelection();
    stopPlacing();
    cancelFrame();
    setCompare(show);
  }

  /** Scroll so a spot on a page (points, as displayed) is in the middle of the view. */
  function scrollTo(page: number, x: number, y: number) {
    const el = scroller.current;
    const figure = document.getElementById(`page-${page}`);
    if (!el || !figure) return;
    const box = (figure.querySelector(".page") as HTMLElement | null) ?? figure;
    const size = doc.pages[page];
    // Measured on screen: the page's place within the scrolled pages, whatever its parents.
    const area = el.getBoundingClientRect();
    const r = box.getBoundingClientRect();
    el.scrollTop += r.top - area.top + (y / size.height) * r.height - el.clientHeight / 3;
    el.scrollLeft += r.left - area.left + (x / size.width) * r.width - el.clientWidth / 2;
  }

  /** Bring a page's top into view (from the pages list). */
  function jumpTo(page: number) {
    const el = scroller.current;
    const figure = document.getElementById(`page-${page}`);
    if (!el || !figure) return;
    const top = el.scrollTop + figure.getBoundingClientRect().top - el.getBoundingClientRect().top - 16;
    const still = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
    el.scrollTo({ top, behavior: still ? "auto" : "smooth" });
  }

  function show(index: number) {
    const change = doc.history[index];
    const where = (change.marks ?? change.areas ?? [])[0];
    if (!where) return;
    const [page, x0, y0, x1, y1] = where;
    startComparing("now");
    requestAnimationFrame(() => scrollTo(page, (x0 + x1) / 2, (y0 + y1) / 2));
  }

  function select(page: number, line: TextLine | null, more: boolean) {
    setImagePick(null);
    if (!line) {
      if (!adding) setPicked([]);
      return;
    }
    setShowPages(false);
    setPicked((current) => {
      const has = current.some((p) => p.page === page && p.line.id === line.id);
      if (more || adding) return has ? current.filter((p) => !(p.page === page && p.line.id === line.id)) : [...current, { page, line }];
      return has && current.length === 1 ? [] : [{ page, line }];
    });
  }

  // Which page is in view: the last one whose top has passed a third of the way down.
  useEffect(() => {
    const el = scroller.current;
    if (!el) return;
    let queued = false;
    const measure = () => {
      queued = false;
      const line = el.getBoundingClientRect().top + el.clientHeight / 3;
      let page = 0;
      for (let i = 0; i < doc.pageCount; i++) {
        const figure = document.getElementById(`page-${i}`);
        if (!figure || figure.getBoundingClientRect().top > line) break;
        page = i;
      }
      setCurrent(page);
    };
    const onScroll = () => {
      if (queued) return;
      queued = true;
      requestAnimationFrame(measure);
    };
    el.addEventListener("scroll", onScroll, { passive: true });
    measure();
    return () => el.removeEventListener("scroll", onScroll);
  }, [doc.pageCount]);

  // Once a line is picked, the user has found their way: no more "New here?" bubble.
  useEffect(() => {
    if (hint && (picked.length || imagePick)) {
      setHint(false);
      markHelpSeen();
    }
  }, [hint, picked.length, imagePick]);

  function startTour() {
    clearSelection();
    stopPlacing();
    cancelFrame();
    setShowChanges(false);
    setShowPages(false);
    setChecking(null);
    setCompare(null);
    setHint(false);
    markHelpSeen();
    setTour(editorSteps(window.matchMedia?.("(max-width: 1023.98px)").matches ?? false));
  }

  function openChanges(open: boolean) {
    setShowPages(false);
    setShowChanges(open);
  }

  // Keep the middle of the view in place when zooming.
  const previousZoom = useRef(zoom);
  useLayoutEffect(() => {
    const el = scroller.current;
    const ratio = zoom / previousZoom.current;
    previousZoom.current = zoom;
    if (!el || ratio === 1) return;
    el.scrollTop = (el.scrollTop + el.clientHeight / 2) * ratio - el.clientHeight / 2;
    el.scrollLeft = (el.scrollLeft + el.clientWidth / 2) * ratio - el.clientWidth / 2;
  }, [zoom]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      // Ctrl+Z undoes, Ctrl+Shift+Z or Ctrl+Y redoes, as in Word (Cmd on a Mac). In a text box
      // they undo typing instead. The key's place is used, so other keyboard layouts work too.
      const inText = e.target instanceof HTMLElement && e.target.closest("input, textarea, [contenteditable], dialog");
      if ((e.ctrlKey || e.metaKey) && !e.altKey && !inText && (e.code === "KeyZ" || e.code === "KeyY")) {
        const again = e.code === "KeyY" || e.shiftKey;
        if (again ? doc.canRedo : doc.canUndo) {
          e.preventDefault();
          if (!busy) void (again ? redo() : undo());
        }
        return;
      }
      // Not while typing, nor from inside the panel (Backspace on its buttons is a Mac's Delete).
      const typing = e.target instanceof HTMLElement && e.target.closest("input, textarea, [contenteditable], .dock, dialog");
      // What's being placed or moved: the arrow keys nudge it a point (Shift: ten), on its page.
      const nudge = { ArrowLeft: [-1, 0], ArrowRight: [1, 0], ArrowUp: [0, -1], ArrowDown: [0, 1] }[e.key];
      if (frame && nudge && !inText && !frameSaving) {
        e.preventDefault();
        const step = e.shiftKey ? 10 : 1;
        const size = doc.pages[frame.page];
        setFrame((now) => {
          if (!now) return now;
          const [x0, y0, x1, y1] = now.box;
          const dx = Math.min(Math.max(nudge[0] * step, -x0), size.width - x1);
          const dy = Math.min(Math.max(nudge[1] * step, -y0), size.height - y1);
          return { ...now, box: [x0 + dx, y0 + dy, x1 + dx, y1 + dy] };
        });
        return;
      }
      if (e.key === "Escape") {
        // A picture being placed or moved is let go first; a picked one stays picked.
        if (frame) {
          if (!frameSaving) cancelFrame();
          return;
        }
        clearSelection();
        stopPlacing();
        setCompare(null);
      } else if ((e.code === "KeyM" || e.key.toLowerCase() === "m") && !typing && !e.ctrlKey && !e.metaKey && !e.altKey) {
        setMagnifier((v) => !v);
      } else if ((e.key === "Delete" || e.key === "Backspace") && !typing && !compare && !frame && (picked.length || imagePick)) {
        e.preventDefault();
        void (picked.length ? remove(picked) : removePicture());
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  });

  const pageWidth = (ptWidth: number) =>
    Math.max(200, Math.min(available - 2 * gutter(available), ptWidth * CSS_PX_PER_PT)) * zoom;

  // While comparing, the pages show a stored version, with every applied change outlined.
  const shownVersion = compare === "original" ? 0 : compare === "now" ? doc.version : undefined;
  const outlines = (page: number) =>
    compare
      ? doc.history.filter((c) => c.applied).flatMap((c) => (c.marks ?? c.areas ?? []).filter((a) => a[0] === page).map((a) => a.slice(1)))
      : [];
  const applied = doc.history.filter((c) => c.applied).length;

  const zoomControls = (
    <div className="zoom-group" role="group" aria-label="Zoom">
      <button
        className="btn btn-ghost btn-icon wide-only"
        aria-pressed={magnifier}
        onClick={() => setMagnifier((v) => !v)}
        aria-label="Magnifier"
        title="Magnifier (M)"
      >
        <Icon d={MAGNIFIER} />
      </button>
      <button
        className="btn btn-ghost btn-icon"
        onClick={() => setZoomStep((z) => Math.max(0, z - 1))}
        disabled={zoomStep === 0}
        aria-label="Zoom out"
        title="Zoom out"
      >
        <Icon d={MINUS} />
      </button>
      <span className="zoom-level" aria-live="polite">
        {Math.round(zoom * 100)}%
      </span>
      <button
        className="btn btn-ghost btn-icon"
        onClick={() => setZoomStep((z) => Math.min(ZOOMS.length - 1, z + 1))}
        disabled={zoomStep === ZOOMS.length - 1}
        aria-label="Zoom in"
        title="Zoom in"
      >
        <Icon d={PLUS} />
      </button>
    </div>
  );

  function togglePlacing() {
    if (placing) stopPlacing();
    else {
      clearSelection();
      cancelFrame();
      setShowChanges(false);
      setPlacing(true);
    }
  }

  function choosePicture() {
    pictureInput.current?.click();
  }

  // What the side panel (wide screens) or the bottom sheet (phones) shows. Nothing: the tools.
  const panel =
    checking !== null && doc.history[checking]?.areas?.length ? (
      <CheckPanel doc={doc} index={checking} busy={busy} onClose={() => setChecking(null)} onUndo={() => void undo()} />
    ) : showChanges ? (
      <ChangesPanel
        doc={doc}
        busy={busy}
        comparing={compare !== null}
        onClose={() => setShowChanges(false)}
        onShow={(k) => {
          setShowChanges(false);
          show(k);
        }}
        onCheck={(k) => {
          setShowChanges(false);
          setChecking(k);
        }}
        onCompare={() => (compare ? setCompare(null) : startComparing())}
        onGoTo={(v) => void goTo(v)}
      />
    ) : showPages ? (
      <PagesPanel
        doc={doc}
        version={shownVersion}
        current={current}
        onClose={() => setShowPages(false)}
        onJump={(page) => {
          jumpTo(page);
          // On phones the panel covers the pages: close it to show the page.
          if (window.matchMedia?.("(max-width: 1023.98px)").matches) setShowPages(false);
        }}
      />
    ) : compare ? null : frame?.mode === "text" ? (
      <MovePanel
        table={!!frame.table}
        count={frame.lines?.length ?? 0}
        saving={frameSaving}
        error={frameError}
        onSave={() => void saveFrame()}
        onCancel={cancelFrame}
      />
    ) : frame ? (
      <PlacePicturePanel
        mode={frame.mode === "add" ? "add" : "move"}
        box={frame.box}
        saving={frameSaving}
        error={frameError}
        onSave={() => void saveFrame()}
        onChooseAnother={frame.mode === "add" ? choosePicture : undefined}
        onCancel={cancelFrame}
      />
    ) : placing ? (
      spot ? (
        <AddTextPanel
          doc={doc}
          spot={spot}
          onClose={stopPlacing}
          onWorking={setWorking}
          onPlan={setSpotPlan}
          onAdded={(outcome) => {
            stopPlacing();
            onChange(outcome.document);
            lightUp(outcome.document, outcome.document.history.at(-1));
            report((outcome.moved ?? 0) > 0 ? "Text added. The text below moved down to make room." : "Text added.", outcome);
            offerCheck(outcome);
          }}
        />
      ) : (
        <section className="panel" aria-label="Add text">
          <div className="panel-head">
            <h2 className="panel-title">Add text</h2>
            <button className="btn btn-ghost btn-icon" onClick={stopPlacing} aria-label="Stop adding text">
              <Icon d={CLOSE} />
            </button>
          </div>
          <p className="panel-lede">
            <span className="wide-only">Click</span>
            <span className="narrow-only">Tap</span> the page where you want the new text. Under a list it becomes the
            next item; between lines, the text below moves down to make room.
          </p>
        </section>
      )
    ) : together ? (
      <TogetherPanel message={together.message} busy={moveBusy} onTogether={() => void moveTogether()} onCancel={() => setTogether(null)} />
    ) : imagePick ? (
      <ImagePanel
        doc={doc}
        page={imagePick.page}
        image={imagePick.image}
        deleting={deleting}
        onClose={() => setImagePick(null)}
        onReplaced={(outcome, pages) => pictureChanged(outcome, pages > 1 ? `Picture replaced on ${pages} pages.` : "Picture replaced.")}
        onMove={startMove}
        onDelete={() => void removePicture()}
        onWorking={setWorking}
      />
    ) : picked.length === 1 && !adding ? (
      <LinePanel
        doc={doc}
        line={picked[0].line}
        page={picked[0].page}
        deleting={deleting}
        onClose={clearSelection}
        onEdited={edited}
        onWorking={setWorking}
        onDelete={() => void remove(picked)}
        onSelectMore={() => setAdding(true)}
        moving={moveBusy}
        onMove={(table) => void startTextMove(picked[0].page, [picked[0].line], table)}
      />
    ) : picked.length > 0 || adding ? (
      <SelectionPanel
        picked={picked}
        deleting={deleting}
        moving={moveBusy}
        onDelete={() => void remove(picked)}
        onMove={() => void startTextMove(picked[0].page, picked.map((p) => p.line), false)}
        onClose={clearSelection}
      />
    ) : null;

  return (
    <div
      className={`viewer${working || deleting ? " viewer--working" : ""}${showText ? " viewer--show-text" : ""}${placing || frame ? " viewer--placing" : ""}${compare ? " viewer--comparing" : ""}`}
    >
      <header className="bar">
        <button
          className="btn btn-ghost btn-icon"
          onClick={() => (unsaved ? setAskClose(true) : onClose())}
          aria-label="Close this PDF and open another"
          title="Close this PDF and open another"
        >
          <Icon d={CLOSE} />
        </button>
        <span className="bar-divider wide-only" aria-hidden="true" />
        <span className="bar-mark wide-only">
          <Mark size={22} />
        </span>
        <div className="doc-title">
          <span className="doc-name" title={doc.name}>
            {doc.name}
          </span>
          <span className="doc-meta">
            {doc.pageCount} {doc.pageCount === 1 ? "page" : "pages"}
            {applied > 0 ? ` · ${applied} ${applied === 1 ? "change" : "changes"}` : ""}
          </span>
        </div>
        <div className="bar-actions">
          <span className="help-wrap">
            <button className="btn btn-ghost btn-icon" onClick={startTour} aria-label="How it works" title="How it works">
              <Icon d={HELP} />
            </button>
            {hint && (
              <button className="help-hint" onClick={startTour}>
                New here? See how it works
              </button>
            )}
          </span>
          <button
            className="btn btn-ghost btn-icon"
            onClick={() => void undo()}
            disabled={busy || !doc.canUndo}
            aria-label="Undo the last change"
            title="Undo"
          >
            <Icon d={UNDO} />
          </button>
          <button
            className="btn btn-ghost btn-icon"
            onClick={() => void redo()}
            disabled={busy || !doc.canRedo}
            aria-label="Redo the change undone last"
            title="Redo"
          >
            <Icon d={REDO} />
          </button>
          <ThemeSwitch className="wide-only" />
          <span className="bar-divider wide-only" aria-hidden="true" />
          <button
            className="btn btn-secondary wide-only"
            aria-pressed={showChanges}
            onClick={() => openChanges(!showChanges)}
            disabled={doc.history.length === 0}
            aria-label={`Changes (${applied})`}
          >
            Changes
            {applied > 0 && <span className="count">{applied}</span>}
          </button>
          <DownloadMenu
            doc={doc}
            onDownloaded={() => setDownloaded(doc.revision)}
            onError={(text) => setNotice({ text, tone: "error" })}
            onNote={(text) => setNotice({ text, tone: "warning" })}
          />
        </div>
      </header>

      <div className={`workspace${doc.pageCount > 1 ? " workspace--rail" : ""}`}>
        {doc.pageCount > 1 && (
          <nav className="page-rail" aria-label="Pages">
            <Thumbnails doc={doc} version={shownVersion} current={current} onJump={jumpTo} />
          </nav>
        )}
        <section className="canvas" aria-label="Pages">
          {doc.isSigned && (
            <p className="banner" role="status">
              This PDF has a digital signature. Editing it will make that signature invalid.
            </p>
          )}

          {compare && (
            <div className="compare-bar" role="group" aria-label="Compare with the original">
              <div className="segmented">
                <button aria-pressed={compare === "original"} onClick={() => setCompare("original")}>
                  Original
                </button>
                <button aria-pressed={compare === "now"} onClick={() => setCompare("now")}>
                  With changes
                </button>
              </div>
              <button className="btn btn-ghost" onClick={() => setCompare(null)}>
                Done
              </button>
            </div>
          )}

          <main className="pages" ref={scroller}>
            <div className="pages-inner">
              {available > 0 &&
                doc.pages.map((p, i) => (
                  <PageView
                    key={i}
                    doc={doc}
                    index={i}
                    width={pageWidth(p.width)}
                    selectedIds={new Set(picked.filter((p) => p.page === i).map((p) => p.line.id))}
                    onSelect={(line, more) => select(i, line, more)}
                    placing={placing || frame !== null}
                    spot={spot?.page === i ? spot : null}
                    preview={spot?.page === i && spotPlan ? spotPlan : null}
                    onPlace={(x, y) => (frame ? placeFrame(i, x, y) : setSpot({ page: i, x, y }))}
                    selectedImageId={imagePick?.page === i ? imagePick.image.id : null}
                    onSelectImage={(image) => selectImage(i, image)}
                    frame={
                      frame?.page === i
                        ? {
                            box: frame.box,
                            src: frame.src,
                            from: frame.from,
                            resizable: frame.mode !== "text",
                            onChange: (box) => setFrame((now) => now && { ...now, box }),
                          }
                        : null
                    }
                    version={shownVersion}
                    outlines={outlines(i)}
                    magnifier={magnifier}
                    flash={flash && { revision: flash.revision, rects: flash.rects.filter((r) => r[0] === i).map((r) => r.slice(1)) }}
                  />
                ))}
            </div>
          </main>

          {doc.pageCount > 1 && (
            <button
              className="page-chip"
              aria-pressed={showPages}
              onClick={() => {
                setShowChanges(false);
                setShowPages((v) => !v);
              }}
              aria-label={`Pages: page ${current + 1} of ${doc.pageCount}`}
            >
              <Icon d={PAGES} size={16} />
              {current + 1} / {doc.pageCount}
            </button>
          )}

          <div className="canvas-zoom">{zoomControls}</div>
        </section>

        <aside className="dock">
          {notice && (
            <div className={`notice notice--${notice.tone}`} role={notice.tone === "error" ? "alert" : "status"}>
              <p>
                {notice.text}
                {notice.detail && <span className="notice-detail">{notice.detail}</span>}
              </p>
              <button className="btn btn-ghost btn-icon" onClick={() => setNotice(null)} aria-label="Dismiss">
                <Icon d={CLOSE} size={16} />
              </button>
            </div>
          )}
          {panel ?? (
            <>
              <HomePanel
                doc={doc}
                placing={placing}
                showText={showText}
                magnifier={magnifier}
                onToggleMagnifier={() => setMagnifier((v) => !v)}
                comparing={compare !== null}
                onAddText={togglePlacing}
                onAddPicture={choosePicture}
                onToggleText={() => setShowText((v) => !v)}
                onShowChanges={() => openChanges(true)}
                onCompare={() => (compare ? setCompare(null) : startComparing())}
              />
              <nav className="toolbar" aria-label="Tools">
                {!compare && (
                  <button className="toolbar-item" aria-pressed={placing} onClick={togglePlacing}>
                    <Icon d={ADD_TEXT} />
                    <span>Add text</span>
                  </button>
                )}
                {!compare && (
                  <button className="toolbar-item" onClick={choosePicture}>
                    <Icon d={PICTURE} />
                    <span>Add picture</span>
                  </button>
                )}
                {!compare && (
                  <button className="toolbar-item" aria-pressed={showText} onClick={() => setShowText((v) => !v)}>
                    <Icon d={BOXES} />
                    <span>Show text</span>
                  </button>
                )}
                <button
                  className="toolbar-item"
                  onClick={() => openChanges(true)}
                  disabled={doc.history.length === 0}
                  aria-label={`Changes (${applied})`}
                >
                  <Icon d={CHANGES} />
                  <span>
                    Changes
                    {applied > 0 && <span className="count">{applied}</span>}
                  </span>
                </button>
                {zoomControls}
              </nav>
            </>
          )}
        </aside>
      </div>

      {askClose && (
        <CloseDialog
          doc={doc}
          onDownload={() => {
            setDownloaded(doc.revision);
            setAskClose(false);
          }}
          onClose={() => {
            setAskClose(false);
            onClose();
          }}
          onCancel={() => setAskClose(false)}
        />
      )}

      <input
        ref={pictureInput}
        type="file"
        accept={PICTURE_TYPES}
        hidden
        onChange={(e) => {
          pictureChosen(e.target.files?.[0]);
          e.target.value = ""; // the same file can be chosen again
        }}
      />

      {tour && <Tour steps={tour} onClose={() => setTour(null)} />}
    </div>
  );
}
