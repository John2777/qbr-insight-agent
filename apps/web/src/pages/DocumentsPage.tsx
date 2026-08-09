import { ChangeEvent, DragEvent, useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { api, statusLabel, uploadPresentation } from "../api";
import type { DocumentItem } from "../types";

export const MAX_UPLOAD_MIB = 10;
export const MAX_UPLOAD_BYTES = MAX_UPLOAD_MIB * 1024 * 1024;

export function validatePresentationFile(file: Pick<File, "name" | "size">): string {
  if (!file.name.toLowerCase().endsWith(".pptx")) return "请选择 .pptx 文件";
  if (file.size > MAX_UPLOAD_BYTES) return `文件不能超过 ${MAX_UPLOAD_MIB} MiB`;
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
    catch (err) { setError(err instanceof Error ? err.message : "上传失败"); }
    finally { setUploading(false); }
  }

  function drop(event: DragEvent) { event.preventDefault(); void selectFile(event.dataTransfer.files[0]); }
  function change(event: ChangeEvent<HTMLInputElement>) { void selectFile(event.target.files?.[0]); }

  return (
    <section className="page">
      <header className="page-header"><div><span className="eyebrow">KNOWLEDGE BASE</span><h1>季度业务文档</h1><p>上传演示文稿，精确解析图表源数据并建立可追溯证据。</p></div></header>
      <button className="upload-zone" onClick={() => inputRef.current?.click()} onDrop={drop} onDragOver={(e) => e.preventDefault()} disabled={uploading}>
        <span className="upload-icon">↑</span><strong>{uploading ? "正在安全上传…" : "拖放 PPTX 到这里"}</strong><small>或点击选择文件 · 最大 {MAX_UPLOAD_MIB} MiB · 宏与外部内容会被拒绝</small>
        <input ref={inputRef} type="file" accept=".pptx" onChange={change} hidden />
      </button>
      {error && <div className="alert error" role="alert">{error}</div>}
      <div className="section-title"><h2>已导入文档</h2><span>{documents.length} 份</span></div>
      <div className="document-list">
        {documents.map((document) => (
          <Link className="document-card" to={`/documents/${document.id}`} key={document.id}>
            <span className="file-mark">P</span>
            <div className="document-main"><strong>{document.title}</strong><span>{document.slide_count || "—"} 页 · {document.review_count || 0} 项待复核</span>
              {document.status === "processing" && <div className="progress"><i style={{ width: `${(document.progress ?? 0) * 100}%` }} /></div>}
            </div>
            <span className={`status ${document.status}`}>{statusLabel(document.status)}</span>
            <span className="arrow">→</span>
          </Link>
        ))}
        {!documents.length && <div className="empty"><strong>还没有文档</strong><p>上传一份含原生图表的 QBR PPTX，开始第一条可验证问答。</p></div>}
      </div>
    </section>
  );
}
