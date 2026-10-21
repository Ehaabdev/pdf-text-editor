export type PageSize = { width: number; height: number };

export type DocInfo = {
  id: string;
  name: string;
  /** Increases with every change and is never reused, so it can key cached images. */
  revision: number;
  edited: boolean;
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

export function pageImageUrl(doc: DocInfo, pageNo: number, width: number): string {
  // The revision makes each URL unique per change, so browsers can cache images safely.
  return `/api/documents/${doc.id}/pages/${pageNo}/image?width=${width}&r=${doc.revision}`;
}

export function downloadUrl(doc: DocInfo): string {
  return `/api/documents/${doc.id}/download?r=${doc.revision}`;
}
