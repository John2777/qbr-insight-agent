import { ChangeEvent, DragEvent, useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { api, statusLabel, uploadPresentation } from "../api";
import type { DocumentItem } from "../types";

export const MAX_UPLOAD_MIB = 10;
export const MAX_UPLOAD_BYTES = MAX_UPLOAD_MIB * 1024 * 1024;

export function validatePresentationFile(file: Pick<File, "name" | "size">): string {
  if (!file.name.toLowerCase().endsWith(".pptx")) return "Select a .pptx file";
  if (file.size > MAX_UPLOAD_BYTES) return `File must not exceed ${MAX_UPLOAD_MIB} MiB`;
  return "";
}

export function DocumentsPage() {
  const [documents, setDocuments] = useState<DocumentItem[]>([]);
  const [uploading, setUploading] = useState(false);
  const [error, setError] = useState("");
  const inputRef = useRef<HTMLInputElement>(null);

  const load = () => api<{ items: DocumentItem[] }>("/api/v1/documents").then((result) => setDocuments(result.items)).catch((err) => setError(err.message));
  useEffect(() => { load(); const timer = window.setInterval(load, 1500); return () => window.clearInterval(timer); }, []);

  async function selectFile(file?: File) {
    if (!file) return;
    setError("");
    const validationError = validatePresentationFile(file);
    if (validationError) { setError(validationError); return; }
    setUploading(true);
    try { await uploadPresentation(file); await load(); }
    catch (err) { setError(err instanceof Error ? err.message : "Upload failed"); }
    finally { setUploading(false); }
  }

  function drop(event: DragEvent) { event.preventDefault(); void selectFile(event.dataTransfer.files[0]); }
  function change(event: ChangeEvent<HTMLInputElement>) { void selectFile(event.target.files?.[0]); }

  return (
    <section className="page">
      <header className="page-header"><div><span className="eyebrow">KNOWLEDGE BASE</span><h1>Quarterly Business Documents</h1><p>Upload presentations to extract chart source data accurately and build traceable evidence.</p></div></header>
      <button className="upload-zone" onClick={() => inputRef.current?.click()} onDrop={drop} onDragOver={(e) => e.preventDefault()} disabled={uploading}>
        <span className="upload-icon">↑</span><strong>{uploading ? "Uploading securely…" : "Drop a PPTX here"}</strong><small>or click to choose a file · {MAX_UPLOAD_MIB} MiB maximum · macros and external content are rejected</small>
        <input ref={inputRef} type="file" accept=".pptx" onChange={change} hidden />
      </button>
      {error && <div className="alert error" role="alert">{error}</div>}
      <div className="section-title"><h2>Imported Documents</h2><span>{documents.length} {documents.length === 1 ? "document" : "documents"}</span></div>
      <div className="document-list">
        {documents.map((document) => (
          <Link className="document-card" to={`/documents/${document.id}`} key={document.id}>
            <span className="file-mark">P</span>
            <div className="document-main"><strong>{document.title}</strong><span>{document.slide_count || "—"} slides · {document.review_count || 0} reviews pending</span>
              {document.status === "processing" && <div className="progress"><i style={{ width: `${(document.progress ?? 0) * 100}%` }} /></div>}
            </div>
            <span className={`status ${document.status}`}>{statusLabel(document.status)}</span>
            <span className="arrow">→</span>
          </Link>
        ))}
        {!documents.length && <div className="empty"><strong>No documents yet</strong><p>Upload a QBR PPTX with native charts to start your first verifiable Q&A.</p></div>}
      </div>
    </section>
  );
}
