import { useEffect, useState } from "react";
import { api } from "../api";

type Analytics = {
  documents: Record<string, number>;
  runs: { total: number; completed: number; failed: number; no_evidence: number; avg_latency_ms: number };
  reviews: Record<string, number>;
  feedback: { total: number; positive_rate: number | null };
  recent_runs: Array<{ id: string; title: string; status: string; created_at: string; warnings: string[] }>;
};

export function AnalyticsPage() {
  const [data, setData] = useState<Analytics | null>(null);
  useEffect(() => { void api<Analytics>("/api/v1/analytics/summary").then(setData); }, []);
  if (!data) return <div className="loading">正在加载分析指标…</div>;
  const documentTotal = Object.values(data.documents).reduce((sum, value) => sum + value, 0);
  const completionRate = data.runs.total ? Math.round(data.runs.completed / data.runs.total * 100) : 0;
  return <section className="page"><header className="page-header"><div><span className="eyebrow">OPERATIONS & QUALITY</span><h1>运行分析</h1><p>用真实运行数据展示问答质量、延迟、知识缺口和复核状态。</p></div></header>
    <div className="metric-grid">
      <article><span>可用文档</span><strong>{documentTotal}</strong><small>{Object.entries(data.documents).map(([key, value]) => `${key} ${value}`).join(" · ") || "暂无"}</small></article>
      <article><span>回答完成率</span><strong>{completionRate}%</strong><small>{data.runs.completed}/{data.runs.total} completed</small></article>
      <article><span>平均端到端延迟</span><strong>{Math.round(data.runs.avg_latency_ms)} ms</strong><small>排队至完成</small></article>
      <article><span>证据不足</span><strong>{data.runs.no_evidence}</strong><small>安全拒答次数</small></article>
      <article><span>待复核</span><strong>{(data.reviews.pending ?? 0) + (data.reviews.in_review ?? 0)}</strong><small>低置信图表</small></article>
      <article><span>正向反馈</span><strong>{data.feedback.positive_rate === null ? "—" : `${Math.round(data.feedback.positive_rate * 100)}%`}</strong><small>{data.feedback.total} 条反馈</small></article>
    </div>
    <div className="section-title"><h2>最近问答运行</h2><span>{data.recent_runs.length} 条</span></div>
    <div className="run-list">{data.recent_runs.map((run) => <article key={run.id}><div><strong>{run.title}</strong><small>{new Date(run.created_at).toLocaleString()}</small></div><span className={`status ${run.status === "completed" ? "ready" : run.status}`}>{run.status}</span>{run.warnings.length > 0 && <small>{run.warnings.join(" · ")}</small>}</article>)}{!data.recent_runs.length && <div className="empty">完成第一条问答后，这里会出现运行记录。</div>}</div>
  </section>;
}
