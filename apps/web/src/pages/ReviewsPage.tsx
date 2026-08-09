import { useEffect, useState } from "react";
import { api } from "../api";

type Review = {
  id: string; reason: string; status: string; document_title: string; slide_no: number;
  created_at: string; assigned_to?: string; original: Record<string, unknown>; corrected?: Record<string, unknown>;
};

function ReviewCard({ item, reload }: { item: Review; reload: () => void }) {
  const [editing, setEditing] = useState(false);
  const [value, setValue] = useState(JSON.stringify(item.corrected ?? item.original, null, 2));
  const [error, setError] = useState("");
  const closed = ["resolved", "dismissed"].includes(item.status);

  async function claim() {
    setError("");
    try { await api(`/api/v1/review-tasks/${item.id}/claim`, { method: "POST" }); setEditing(true); reload(); }
    catch (err) { setError(err instanceof Error ? err.message : "领取失败"); }
  }

  async function resolve(resolution: "resolved" | "dismissed") {
    setError("");
    try {
      const corrected = resolution === "resolved" ? JSON.parse(value) as Record<string, unknown> : undefined;
      await api(`/api/v1/review-tasks/${item.id}/resolve`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ resolution, corrected })
      });
      setEditing(false); reload();
    } catch (err) { setError(err instanceof Error ? err.message : "提交失败，请检查 JSON"); }
  }

  return <article className="review-card">
    <header><div><strong>{item.document_title} · 第 {item.slide_no} 页</strong><span>{item.reason}</span></div><span className={`status ${closed ? "ready" : "partial"}`}>{item.status}</span></header>
    {!closed && !editing && <button className="secondary-button" onClick={() => void claim()}>{item.status === "in_review" ? "继续复核" : "领取复核"}</button>}
    {!closed && editing && <div className="review-editor"><p>修正结构化图表 JSON；提交后会保留 revision 并局部重建索引。</p><textarea aria-label="修正后的图表 JSON" value={value} onChange={(event) => setValue(event.target.value)} /><div><button className="primary-button" onClick={() => void resolve("resolved")}>保存修正</button><button className="secondary-button" onClick={() => void resolve("dismissed")}>无需修正</button></div></div>}
    {error && <div className="alert error" role="alert">{error}</div>}
  </article>;
}

export function ReviewsPage() {
  const [items, setItems] = useState<Review[]>([]);
  const load = () => { void api<{ items: Review[] }>("/api/v1/review-tasks").then((data) => setItems(data.items)); };
  useEffect(load, []);
  return <section className="page"><header className="page-header"><div><span className="eyebrow">QUALITY CONTROL</span><h1>证据复核</h1><p>领取低置信图表、修正结构数据并留下不可变 revision。</p></div></header><div className="review-list">{items.map((item) => <ReviewCard item={item} reload={load} key={item.id} />)}{!items.length && <div className="empty"><strong>当前没有复核项</strong><p>解析器发现低置信或不完整图表时，会自动加入这里。</p></div>}</div></section>;
}
