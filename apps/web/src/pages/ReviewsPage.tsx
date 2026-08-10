import { useEffect, useState } from "react";
import { api, statusLabel } from "../api";

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
    catch (err) { setError(err instanceof Error ? err.message : "Failed to claim review"); }
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
    } catch (err) { setError(err instanceof Error ? err.message : "Submission failed. Check the JSON and try again."); }
  }

  return <article className="review-card">
    <header><div><strong>{item.document_title} · Slide {item.slide_no}</strong><span>{item.reason}</span></div><span className={`status ${closed ? "ready" : "partial"}`}>{statusLabel(item.status)}</span></header>
    {!closed && !editing && <button className="secondary-button" onClick={() => void claim()}>{item.status === "in_review" ? "Continue review" : "Claim review"}</button>}
    {!closed && editing && <div className="review-editor"><p>Correct the structured chart JSON. Submission preserves a revision and rebuilds the affected index.</p><textarea aria-label="Corrected chart JSON" value={value} onChange={(event) => setValue(event.target.value)} /><div><button className="primary-button" onClick={() => void resolve("resolved")}>Save changes</button><button className="secondary-button" onClick={() => void resolve("dismissed")}>No changes needed</button></div></div>}
    {error && <div className="alert error" role="alert">{error}</div>}
  </article>;
}

export function ReviewsPage() {
  const [items, setItems] = useState<Review[]>([]);
  const load = () => { void api<{ items: Review[] }>("/api/v1/review-tasks").then((data) => setItems(data.items)); };
  useEffect(load, []);
  return <section className="page"><header className="page-header"><div><span className="eyebrow">QUALITY CONTROL</span><h1>Evidence Review</h1><p>Claim low-confidence charts, correct structured data, and preserve an immutable revision.</p></div></header><div className="review-list">{items.map((item) => <ReviewCard item={item} reload={load} key={item.id} />)}{!items.length && <div className="empty"><strong>No items to review</strong><p>Low-confidence or incomplete charts will appear here automatically.</p></div>}</div></section>;
}
