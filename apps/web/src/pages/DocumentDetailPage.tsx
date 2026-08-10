import { useEffect, useRef, useState } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom";
import { api, statusLabel } from "../api";
import { ConfidenceBadge } from "../components/ConfidenceBadge";
import { SlideCanvas } from "../components/SlideCanvas";
import type { DocumentItem, SlideDetail, SlideItem } from "../types";

type DocumentDetail = DocumentItem & { versions: Array<{ id: string; active_parser_run_id?: string }> };

export function DocumentDetailPage() {
  const { documentId } = useParams();
  const navigate = useNavigate();
  const [params, setParams] = useSearchParams();
  const [document, setDocument] = useState<DocumentDetail | null>(null);
  const [slides, setSlides] = useState<SlideItem[]>([]);
  const [current, setCurrent] = useState<SlideDetail | null>(null);
  const [tab, setTab] = useState<"content" | "charts" | "notes">("content");
  const [deleting, setDeleting] = useState(false);
  const [deleteError, setDeleteError] = useState("");
  const slideDetailRequests = useRef(new Map<string, Promise<SlideDetail>>());

  useEffect(() => { if (!documentId) return; api<DocumentDetail>(`/api/v1/documents/${documentId}`).then(setDocument); }, [documentId]);
  useEffect(() => {
    const version = document?.versions[0]; if (!version?.active_parser_run_id) return;
    api<{ items: SlideItem[] }>(`/api/v1/document-versions/${version.id}/slides`).then(({ items }) => setSlides(items));
  }, [document]);

  const selectedSlideNo = Number(params.get("slide"));
  useEffect(() => {
    const selected = slides.find((slide) => slide.slide_no === selectedSlideNo) ?? slides[0];
    if (!selected) return;
    let active = true;
    let request = slideDetailRequests.current.get(selected.id);
    if (!request) {
      request = api<SlideDetail>(`/api/v1/slides/${selected.id}`);
      slideDetailRequests.current.set(selected.id, request);
      void request.catch(() => slideDetailRequests.current.delete(selected.id));
    }
    void request.then((slide) => { if (active) setCurrent(slide); }, () => undefined);
    return () => { active = false; };
  }, [selectedSlideNo, slides]);

  function choose(slide: SlideItem) { setParams({ slide: String(slide.slide_no) }); }

  async function purge() {
    if (!document || deleting) return;
    const confirmed = window.confirm(
      `Permanently delete “${document.title}”?\n\nThis removes the original PPT, parsed results, search index, and every Q&A that references or is scoped to this PPT. This action cannot be undone.`
    );
    if (!confirmed) return;
    setDeleting(true);
    setDeleteError("");
    try {
      const result = await api<{ status: "completed" | "partial" }>(`/api/v1/documents/${document.id}/purge`, { method: "DELETE" });
      if (result.status !== "completed") {
        throw new Error("Database records were deleted, but some files or vector indexes remain. Try again to finish cleanup.");
      }
      navigate("/documents", { replace: true });
    } catch (error) {
      setDeleteError(error instanceof Error ? error.message : "Delete failed. Try again.");
    } finally {
      setDeleting(false);
    }
  }

  if (!document) return <div className="loading">Loading document…</div>;
  const header = <header className="detail-header"><div><Link to="/documents" className="back">← Document Library</Link><h1>{document.title}</h1></div><div className="detail-actions">{document.versions[0]?.active_parser_run_id && <Link className="primary-button" to={`/chat?document=${document.id}`}>Ask about this document</Link>}<button className="danger-button" disabled={deleting} onClick={() => void purge()}>{deleting ? "Permanently deleting…" : "Permanently delete"}</button></div></header>;
  if (!document.versions[0]?.active_parser_run_id) return <section className="detail-page">{header}{deleteError && <div className="alert error detail-delete-error" role="alert">{deleteError}</div>}<div className="processing-card"><div className="spinner"/><h2>{statusLabel(document.status)}</h2><p>Slides and structured charts will appear here when parsing is complete.</p></div></section>;

  return (
    <section className="detail-page">
      {header}
      {deleteError && <div className="alert error detail-delete-error" role="alert">{deleteError}</div>}
      <div className="detail-grid">
        <aside className="slide-nav" aria-label="Slide list">{slides.map((slide) => <button key={slide.id} onClick={() => choose(slide)} className={current?.id === slide.id ? "active" : ""}><span>{slide.slide_no}</span><img src={slide.thumbnail_url ?? slide.preview_url} alt={`Slide ${slide.slide_no} thumbnail`} loading="lazy" decoding="async" width="320" height="180"/><small>{slide.title || "Untitled"}</small></button>)}</aside>
        <div className="preview-pane">{current && <><div className="preview-toolbar"><strong>Slide {current.slide_no}</strong><span>Quality {Math.round(current.quality_score * 100)}%</span></div><SlideCanvas slide={current}/></>}</div>
        <aside className="structured-pane">
          <div className="tabs"><button className={tab === "content" ? "active" : ""} onClick={() => setTab("content")}>Content</button><button className={tab === "charts" ? "active" : ""} onClick={() => setTab("charts")}>Charts</button><button className={tab === "notes" ? "active" : ""} onClick={() => setTab("notes")}>Notes</button></div>
          {tab === "content" && current?.elements.filter((e) => e.element_type !== "notes").map((element) => <article className="element-card" key={element.id}><div><span>{element.element_type}</span><ConfidenceBadge confidence={element.confidence}/></div><p>{element.text_content || "Structured element"}</p></article>)}
          {tab === "charts" && current?.charts.map((chart) => <article className="chart-card" key={chart.id}><h3>{chart.title || "Untitled chart"}</h3><ConfidenceBadge confidence={chart.confidence} source={chart.source_kind}/>{chart.series.map((series) => <div key={series.id} className="series-table"><strong>{series.name}</strong><table><tbody>{series.points.map((point) => <tr key={point.id}><td>{point.category}</td><td>{point.display_value ?? point.y_value ?? "—"}</td></tr>)}</tbody></table></div>)}</article>)}
          {tab === "notes" && <div className="notes">{current?.notes_text || "This slide has no speaker notes."}</div>}
        </aside>
      </div>
    </section>
  );
}
