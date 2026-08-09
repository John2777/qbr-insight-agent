import { useEffect, useState } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";
import { api, statusLabel } from "../api";
import { ConfidenceBadge } from "../components/ConfidenceBadge";
import { SlideCanvas } from "../components/SlideCanvas";
import type { DocumentItem, SlideDetail, SlideItem } from "../types";

type DocumentDetail = DocumentItem & { versions: Array<{ id: string; active_parser_run_id?: string }> };

export function DocumentDetailPage() {
  const { documentId } = useParams();
  const [params, setParams] = useSearchParams();
  const [document, setDocument] = useState<DocumentDetail | null>(null);
  const [slides, setSlides] = useState<SlideItem[]>([]);
  const [current, setCurrent] = useState<SlideDetail | null>(null);
  const [tab, setTab] = useState<"content" | "charts" | "notes">("content");

  useEffect(() => { if (!documentId) return; api<DocumentDetail>(`/api/v1/documents/${documentId}`).then(setDocument); }, [documentId]);
  useEffect(() => {
    const version = document?.versions[0]; if (!version?.active_parser_run_id) return;
    api<{ items: SlideItem[] }>(`/api/v1/document-versions/${version.id}/slides`).then(({ items }) => {
      setSlides(items); const wanted = Number(params.get("slide")); const selected = items.find((s) => s.slide_no === wanted) ?? items[0];
      if (selected) api<SlideDetail>(`/api/v1/slides/${selected.id}`).then(setCurrent);
    });
  }, [document, params]);

  function choose(slide: SlideItem) { setParams({ slide: String(slide.slide_no) }); api<SlideDetail>(`/api/v1/slides/${slide.id}`).then(setCurrent); }
  if (!document) return <div className="loading">正在加载文档…</div>;
  if (!document.versions[0]?.active_parser_run_id) return <section className="page"><Link to="/documents">← 返回文档库</Link><div className="processing-card"><div className="spinner"/><h2>{statusLabel(document.status)}</h2><p>解析完成后，本页会显示幻灯片和结构化图表。</p></div></section>;

  return (
    <section className="detail-page">
      <header className="detail-header"><div><Link to="/documents" className="back">← 文档库</Link><h1>{document.title}</h1></div><Link className="primary-button" to={`/chat?document=${document.id}`}>基于此文档提问</Link></header>
      <div className="detail-grid">
        <aside className="slide-nav" aria-label="幻灯片列表">{slides.map((slide) => <button key={slide.id} onClick={() => choose(slide)} className={current?.id === slide.id ? "active" : ""}><span>{slide.slide_no}</span><img src={slide.preview_url} alt=""/><small>{slide.title || "无标题"}</small></button>)}</aside>
        <div className="preview-pane">{current && <><div className="preview-toolbar"><strong>第 {current.slide_no} 页</strong><span>质量 {Math.round(current.quality_score * 100)}%</span></div><SlideCanvas slide={current}/></>}</div>
        <aside className="structured-pane">
          <div className="tabs"><button className={tab === "content" ? "active" : ""} onClick={() => setTab("content")}>内容</button><button className={tab === "charts" ? "active" : ""} onClick={() => setTab("charts")}>图表</button><button className={tab === "notes" ? "active" : ""} onClick={() => setTab("notes")}>备注</button></div>
          {tab === "content" && current?.elements.filter((e) => e.element_type !== "notes").map((element) => <article className="element-card" key={element.id}><div><span>{element.element_type}</span><ConfidenceBadge confidence={element.confidence}/></div><p>{element.text_content || "结构化元素"}</p></article>)}
          {tab === "charts" && current?.charts.map((chart) => <article className="chart-card" key={chart.id}><h3>{chart.title || "未命名图表"}</h3><ConfidenceBadge confidence={chart.confidence} source={chart.source_kind}/>{chart.series.map((series) => <div key={series.id} className="series-table"><strong>{series.name}</strong><table><tbody>{series.points.map((point) => <tr key={point.id}><td>{point.category}</td><td>{point.display_value ?? point.y_value ?? "—"}</td></tr>)}</tbody></table></div>)}</article>)}
          {tab === "notes" && <div className="notes">{current?.notes_text || "此页没有演讲者备注。"}</div>}
        </aside>
      </div>
    </section>
  );
}

