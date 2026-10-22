import { useEffect, useState } from "react";
import { ApiError, deleteDocument, getDocument, type DocInfo } from "./api";
import UploadScreen from "./components/UploadScreen";
import Viewer from "./components/Viewer";

const SESSION_KEY = "pdfedit.docId";

function rememberDoc(id: string | null) {
  try {
    if (id) sessionStorage.setItem(SESSION_KEY, id);
    else sessionStorage.removeItem(SESSION_KEY);
  } catch {
    // Storage can be unavailable (private mode); reloading then just starts over.
  }
}

function recalledDoc(): string | null {
  try {
    return sessionStorage.getItem(SESSION_KEY);
  } catch {
    return null;
  }
}

export default function App() {
  const [doc, setDoc] = useState<DocInfo | null>(null);
  const [restoring, setRestoring] = useState(() => recalledDoc() !== null);
  const [notice, setNotice] = useState<string | null>(null);

  // After a reload, reopen the file that was open in this tab.
  useEffect(() => {
    const id = recalledDoc();
    if (!id) return;
    getDocument(id)
      .then(setDoc)
      .catch((e) => {
        rememberDoc(null);
        if (e instanceof ApiError && e.status === 404) setNotice(e.message);
      })
      .finally(() => setRestoring(false));
  }, []);

  function open(d: DocInfo) {
    rememberDoc(d.id);
    setNotice(null);
    setDoc(d);
  }

  function close() {
    if (doc) void deleteDocument(doc.id);
    rememberDoc(null);
    setDoc(null);
  }

  return (
    <>
      {/* The glowing background everything floats on. It drifts only on the start screen. */}
      <div className={`aurora${doc || restoring ? "" : " aurora--live"}`} aria-hidden="true" />
      {restoring ? (
        <div className="desk" aria-busy="true" />
      ) : doc ? (
        <Viewer doc={doc} onClose={close} onChange={setDoc} />
      ) : (
        <UploadScreen onOpen={open} notice={notice} />
      )}
    </>
  );
}
