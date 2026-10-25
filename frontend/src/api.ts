export type PageSize = { width: number; height: number };

/** A font that wasn't available, and the look-alike that drew the new letters instead. */
export type Lookalike = { font: string; substitute: string; sameWidths: boolean };

/** One change, as listed: what it was and where (page, x0, y0, x1, y1 in points). */
export type Change = {
  kind: "edit" | "delete" | "add" | "image";
  summary: string;
  pages: number[];
  areas: [number, number, number, number, number][];
  /** The same changes drawn tight (the old and new lines), for outlining on the page. */
  marks?: [number, number, number, number, number][];
  warnings: string[];
  lookalikes?: Lookalike[];
  /** False once undone (it can be redone until the next change). */
  applied: boolean;
};

export type DocInfo = {
  id: string;
  name: string;
  /** Increases with every change and is never reused, so it can key cached images. */
  revision: number;
  edited: boolean;
  canUndo: boolean;
  canRedo: boolean;
  /** Which stored version is current: 0 is the original, k the one after history[k-1]. */
  version: number;
  history: Change[];
  pageCount: number;
  pages: PageSize[];
  isSigned: boolean;
};

export class ApiError extends Error {
  constructor(message: string, readonly status: number) {
    super(message);
  }
}

async function errorFrom(res: Response): Promise<ApiError> {
  let message = `Something went wrong (error ${res.status}). Try again.`;
  try {
    const body = await res.json();
    if (typeof body.detail === "string") message = body.detail;
  } catch {
    // Not JSON: keep the generic message.
  }
  return new ApiError(message, res.status);
}

/** Upload with progress. fetch() can't report upload progress, so this uses XHR. */
export function uploadPdf(file: File, onProgress: (fraction: number) => void): Promise<DocInfo> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", "/api/documents");
    xhr.responseType = "json";
    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable) onProgress(e.loaded / e.total);
    };
    xhr.onload = () => {
      if (xhr.status === 201) return resolve(xhr.response as DocInfo);
      const detail = xhr.response?.detail;
      reject(
        new ApiError(
          typeof detail === "string" ? detail : `Upload failed (error ${xhr.status}). Try again.`,
          xhr.status,
        ),
      );
    };
    xhr.onerror = () => reject(new ApiError("Couldn't reach the server. Check your connection and try again.", 0));
    const form = new FormData();
    form.append("file", file);
    xhr.send(form);
  });
}

export async function getDocument(id: string): Promise<DocInfo> {
  const res = await fetch(`/api/documents/${encodeURIComponent(id)}`);
  if (!res.ok) throw await errorFrom(res);
  return res.json();
}

export async function deleteDocument(id: string): Promise<void> {
  await fetch(`/api/documents/${encodeURIComponent(id)}`, { method: "DELETE" });
}

/** The page as an image: the current version, or a stored one (0 is the original). */
export function pageImageUrl(doc: DocInfo, pageNo: number, width: number, version?: number): string {
  const base = `/api/documents/${doc.id}/pages/${pageNo}/image?width=${width}`;
  // The original never changes. Other URLs carry the revision, which every change bumps,
  // so browsers can cache images safely (a version number is reused after an undo).
  if (version === 0) return `${base}&version=0`;
  return `${base}&r=${doc.revision}` + (version === undefined ? "" : `&version=${version}`);
}

/** Part of a page (points, as displayed) in a given version, for a close look. */
export function areaImageUrl(doc: DocInfo, pageNo: number, version: number, area: number[], width: number): string {
  const clip = area.map((v) => v.toFixed(2)).join(",");
  return `${pageImageUrl(doc, pageNo, width, version)}&clip=${clip}`;
}

export type TextSpan = {
  text: string;
  font: string;
  size: number;
  color: string;
  bold: boolean;
  italic: boolean;
  superscript: boolean;
};

export type TextLine = {
  id: string;
  /** Typing order. */
  text: string;
  /** Text with direction marks so it shows in the same order as on the page. */
  display: string;
  dir: "ltr" | "rtl";
  /** [x0, y0, x1, y1] in PDF points on the page as displayed. */
  bbox: [number, number, number, number];
  font: string;
  size: number;
  color: string;
  bold: boolean;
  italic: boolean;
  spans: TextSpan[];
  /** The PDF's own text for this line is partly wrong (a known Word problem). */
  uncertain: boolean;
  /** In a list: the marker ("4.", "•") a new item gets below the line (its item's last
   *  line) or above it (its item's first line). */
  item?: { below: string | null; above: string | null } | null;
};

export type PageLines = { width: number; height: number; lines: TextLine[] };

const linesCache = new Map<string, Promise<PageLines>>();

export function getLines(doc: DocInfo, pageNo: number): Promise<PageLines> {
  const key = `${doc.id}:${doc.revision}:${pageNo}`;
  let p = linesCache.get(key);
  if (!p) {
    p = fetch(`/api/documents/${doc.id}/pages/${pageNo}/lines?r=${doc.revision}`).then(async (res) => {
      if (!res.ok) throw await errorFrom(res);
      return res.json();
    });
    p.catch(() => linesCache.delete(key));
    linesCache.set(key, p);
  }
  return p;
}

/** A picture on a page. `bbox` in points, as displayed; `pages`: how many pages show the
 *  same picture (a logo in the header); `background`: it covers most of the page. */
export type PageImage = {
  id: string;
  bbox: [number, number, number, number];
  kind: "image" | "inline" | "drawing";
  width: number;
  height: number;
  pages: number;
  background: boolean;
};

const imagesCache = new Map<string, Promise<PageImage[]>>();

export function getImages(doc: DocInfo, pageNo: number): Promise<PageImage[]> {
  const key = `${doc.id}:${doc.revision}:${pageNo}`;
  let p = imagesCache.get(key);
  if (!p) {
    p = fetch(`/api/documents/${doc.id}/pages/${pageNo}/images?r=${doc.revision}`).then(async (res) => {
      if (!res.ok) throw await errorFrom(res);
      return (await res.json()).images;
    });
    p.catch(() => imagesCache.delete(key));
    imagesCache.set(key, p);
  }
  return p;
}

/** One picture alone, see-through around it (for showing it while it's moved). */
export function pictureUrl(doc: DocInfo, page: number, image: PageImage, width: number): string {
  return `/api/documents/${doc.id}/pages/${page}/images/${encodeURIComponent(image.id)}/picture?width=${width}&r=${doc.revision}`;
}

async function postForm<T>(url: string, form: FormData): Promise<T> {
  let res: Response;
  try {
    res = await fetch(url, { method: "POST", body: form });
  } catch {
    throw new ApiError("Couldn't reach the server. Check your connection and try again.", 0);
  }
  if (!res.ok) throw await errorFrom(res);
  return res.json();
}

const imageRef = (doc: DocInfo, page: number, image: PageImage) => ({ revision: doc.revision, page, imageId: image.id, bbox: image.bbox });

export function deleteImage(doc: DocInfo, page: number, image: PageImage): Promise<EditOutcome> {
  return postJson(`/api/documents/${doc.id}/images/delete`, imageRef(doc, page, image));
}

/** Move or resize a picture to `box` (points, as displayed). */
export function moveImage(doc: DocInfo, page: number, image: PageImage, box: number[]): Promise<EditOutcome> {
  return postJson(`/api/documents/${doc.id}/images/move`, { ...imageRef(doc, page, image), box });
}

/** Show `file` where the picture is: all of it ("fit"), covering the place ("fill"), or
 *  stretched to it; on every page the picture is on, with `everywhere`. */
export function replaceImage(doc: DocInfo, page: number, image: PageImage, file: File, mode: "fit" | "fill" | "stretch", everywhere: boolean): Promise<EditOutcome> {
  const form = new FormData();
  form.append("file", file);
  form.append("revision", String(doc.revision));
  form.append("page", String(page));
  form.append("imageId", image.id);
  form.append("bbox", image.bbox.join(","));
  form.append("mode", mode);
  form.append("everywhere", String(everywhere));
  return postForm(`/api/documents/${doc.id}/images/replace`, form);
}

/** Put `file` on the page, on `box` (points, as displayed). */
export function addImage(doc: DocInfo, page: number, file: File, box: number[]): Promise<EditOutcome> {
  const form = new FormData();
  form.append("file", file);
  form.append("revision", String(doc.revision));
  form.append("page", String(page));
  form.append("box", box.map((v) => v.toFixed(2)).join(","));
  return postForm(`/api/documents/${doc.id}/images/add`, form);
}

/** `moved`: lines that moved down to make room, or up into a deleted line's space. */
export type EditOutcome = { document: DocInfo; warnings: string[]; lookalikes?: Lookalike[]; moved?: number };

async function postJson<T>(url: string, body: unknown): Promise<T> {
  let res: Response;
  try {
    res = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  } catch {
    throw new ApiError("Couldn't reach the server. Check your connection and try again.", 0);
  }
  if (!res.ok) throw await errorFrom(res);
  return res.json();
}

/** Replace the text of one line. The server refuses if the document or the line changed. */
export function editLine(doc: DocInfo, page: number, line: TextLine, newText: string): Promise<EditOutcome> {
  return postJson(`/api/documents/${doc.id}/edits`, {
    revision: doc.revision,
    page,
    lineId: line.id,
    oldText: line.text,
    newText,
  });
}

/** Add a new line below or above a line, in that line's font, size and color. */
/** Add a line below or above a line. With `item`, as a new item of the line's list:
 *  numbered on, the items after it renumbered. */
export function addLine(doc: DocInfo, page: number, line: TextLine, text: string, where: "below" | "above", item = false): Promise<EditOutcome> {
  return postJson(`/api/documents/${doc.id}/additions`, {
    revision: doc.revision,
    page,
    lineId: line.id,
    oldText: line.text,
    text,
    where,
    item,
  });
}

/** What adding text at a clicked spot would do, worked out by the server before anything
 *  is written: a new list item or a new line next to a line (the text below moving down),
 *  text joining a line on its row, or text where clicked. */
export type SpotPlan = {
  kind: "item" | "line" | "join" | "free";
  lineId: string | null;
  oldText: string | null;
  where: "below" | "above";
  marker: string | null;
  /** Where it goes, in points as displayed: for item and line, the new line's band. */
  box: [number, number, number, number];
  label: string;
};

export async function spotPlan(doc: DocInfo, page: number, x: number, y: number): Promise<SpotPlan> {
  let res: Response;
  try {
    res = await fetch(`/api/documents/${doc.id}/pages/${page}/spot?x=${x.toFixed(2)}&y=${y.toFixed(2)}&r=${doc.revision}`);
  } catch {
    throw new ApiError("Couldn't reach the server. Check your connection and try again.", 0);
  }
  if (!res.ok) throw await errorFrom(res);
  return res.json();
}

/** Add text at a point on the page (in points, as displayed), in the style of a line. */
export function addTextAt(doc: DocInfo, page: number, styleOf: TextLine, text: string, at: [number, number]): Promise<EditOutcome> {
  return postJson(`/api/documents/${doc.id}/additions`, {
    revision: doc.revision,
    page,
    lineId: styleOf.id,
    oldText: styleOf.text,
    text,
    at,
  });
}

export type PickedLine = { page: number; line: TextLine };

/** Delete whole lines, on any pages, as one change: one Undo brings them all back. */
export function deleteLines(doc: DocInfo, lines: PickedLine[]): Promise<EditOutcome> {
  return postJson(`/api/documents/${doc.id}/deletions`, {
    revision: doc.revision,
    lines: lines.map(({ page, line }) => ({ page, lineId: line.id, oldText: line.text })),
  });
}

export function undoEdit(doc: DocInfo): Promise<DocInfo> {
  return postJson(`/api/documents/${doc.id}/undo`, { revision: doc.revision });
}

export function redoEdit(doc: DocInfo): Promise<DocInfo> {
  return postJson(`/api/documents/${doc.id}/redo`, { revision: doc.revision });
}

/** Make a stored version current: 0 is the original. Later ones can still be redone. */
export function goToVersion(doc: DocInfo, version: number): Promise<DocInfo> {
  return postJson(`/api/documents/${doc.id}/versions/${version}`, { revision: doc.revision });
}

export function downloadUrl(doc: DocInfo): string {
  return `/api/documents/${doc.id}/download?r=${doc.revision}`;
}

/** Download as…: the formats the server can make from the PDF. */
export type ExportFormat = "docx" | "pptx" | "pptx-pictures" | "xlsx" | "jpg" | "png";

/** The PDF made into another kind of file, with the name the server gives it. */
export async function exportDocument(doc: DocInfo, format: ExportFormat): Promise<{ blob: Blob; filename: string; note: string | null }> {
  let res: Response;
  try {
    res = await fetch(`/api/documents/${doc.id}/export?format=${format}&r=${doc.revision}`);
  } catch {
    throw new ApiError("Couldn't reach the server. Check your connection and try again.", 0);
  }
  if (!res.ok) throw await errorFrom(res);
  const header = res.headers.get("Content-Disposition") ?? "";
  const encoded = /filename\*=utf-8''([^;]+)/i.exec(header);
  const plain = /filename="([^"]+)"/i.exec(header);
  const filename = encoded ? decodeURIComponent(encoded[1]) : plain ? plain[1] : `${doc.name.replace(/\.pdf$/i, "")}.${format}`;
  // Something to tell the person about the file (lines to check, say).
  const note = res.headers.get("X-Export-Note");
  return { blob: await res.blob(), filename, note: note ? decodeURIComponent(note) : null };
}
