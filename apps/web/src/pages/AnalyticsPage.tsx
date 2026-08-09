import { useEffect, useState } from "react";
import { api } from "../api";
import { CONVERSATIONS_CHANGED_EVENT } from "../conversationEvents";

type Analytics = {
  documents: Record<string, number>;
  runs: { total: number; completed: number; failed: number; no_evidence: number; degraded: number; avg_latency_ms: number };
  reviews: Record<string, number>;
  feedback: { total: number; positive_rate: number | null };
  recent_runs: Array<{
    id: string;
    title: string;
    status: string;
    created_at: string;
    warnings: string[];
    warning_details?: WarningDetail[];
  }>;
};

type WarningDetail = {
  code: string;
  severity: "info" | "warning" | "degraded" | "error";
  category: string;
  label: string;
  description: string;
};

const LEGACY_WARNING_LABELS: Record<string, Pick<WarningDetail, "severity" | "label">> = {
  SYNTHETIC_DATA_SIGNAL: { severity: "info", label: "文档含模拟数据" },
  INSUFFICIENT_EVIDENCE: { severity: "warning", label: "证据不足" },
  QUERY_PLANNER_PROVIDER_ERROR: { severity: "degraded", label: "查询规划已降级" },
  LLM_PROVIDER_ERROR: { severity: "degraded", label: "模型生成已降级" },
};

function detailsFor(run: Analytics["recent_runs"][number]): WarningDetail[] {
  if (run.warning_details) return run.warning_details;
  return run.warnings.map((code) => ({
    code,
    category: "legacy",
    description: "运行诊断信号；部署后端新版本后可查看完整说明。",
    ...(LEGACY_WARNING_LABELS[code] ?? { severity: "warning" as const, label: "运行提示" }),
  }));
}

export function AnalyticsPage() {
  const [data, setData] = useState<Analytics | null>(null);
  useEffect(() => {
    let active = true;
    const load = () => {
      void api<Analytics>("/api/v1/analytics/summary")
        .then((result) => { if (active) setData(result); })
        .catch(() => undefined);
    };
    load();
    window.addEventListener(CONVERSATIONS_CHANGED_EVENT, load);
    return () => {
      active = false;
      window.removeEventListener(CONVERSATIONS_CHANGED_EVENT, load);
    };
  }, []);
  if (!data) return <div className="loading">正在加载分析指标…</div>;
  const documentTotal = Object.values(data.documents).reduce((sum, value) => sum + value, 0);
  const completionRate = data.runs.total ? Math.round(data.runs.completed / data.runs.total * 100) : 0;
  return <section className="page"><header className="page-header"><div><span className="eyebrow">OPERATIONS & QUALITY</span><h1>运行分析</h1><p>用真实运行数据展示问答质量、延迟、知识缺口和复核状态。</p></div></header>
    <div className="metric-grid">
      <article><span>可用文档</span><strong>{documentTotal}</strong><small>{Object.entries(data.documents).map(([key, value]) => `${key} ${value}`).join(" · ") || "暂无"}</small></article>
      <article><span>回答完成率</span><strong>{completionRate}%</strong><small>{data.runs.completed}/{data.runs.total} 已完成 · {data.runs.degraded ?? 0} 次自动降级</small></article>
      <article><span>平均端到端延迟</span><strong>{Math.round(data.runs.avg_latency_ms)} ms</strong><small>排队至完成</small></article>
      <article><span>证据不足</span><strong>{data.runs.no_evidence}</strong><small>安全拒答次数</small></article>
      <article><span>待复核</span><strong>{(data.reviews.pending ?? 0) + (data.reviews.in_review ?? 0)}</strong><small>低置信图表</small></article>
      <article><span>正向反馈</span><strong>{data.feedback.positive_rate === null ? "—" : `${Math.round(data.feedback.positive_rate * 100)}%`}</strong><small>{data.feedback.total} 条反馈</small></article>
    </div>
    <div className="section-title"><h2>最近问答运行</h2><span>{data.recent_runs.length} 条</span></div>
    <div className="run-list">{data.recent_runs.map((run) => {
      const warningDetails = detailsFor(run);
      return <article key={run.id}>
        <div className="run-summary"><strong>{run.title}</strong><small>{new Date(run.created_at).toLocaleString()}</small></div>
        <span className={`status ${run.status === "completed" ? "ready" : run.status}`}>{run.status === "completed" ? "已完成" : run.status}</span>
        {warningDetails.length > 0 && <div className="run-signals" aria-label="运行提示">
          {warningDetails.map((warning) => <span
            className={`run-signal ${warning.severity}`}
            key={warning.code}
            title={`${warning.description} (${warning.code})`}
          >{warning.label}</span>)}
        </div>}
      </article>;
    })}{!data.recent_runs.length && <div className="empty">完成第一条问答后，这里会出现运行记录。</div>}</div>
  </section>;
}
