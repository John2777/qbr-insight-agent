import { useEffect, useState } from "react";
import { api, statusLabel } from "../api";
import { CONVERSATIONS_CHANGED_EVENT } from "../conversationEvents";

type Analytics = {
  documents: Record<string, number>;
  runs: { total: number; completed: number; failed: number; no_evidence: number; degraded: number; avg_latency_ms: number };
  reviews: Record<string, number>;
  feedback: { total: number; positive_rate: number | null };
  recent_runs: Array<{
    id: string;
    title: string;
    question?: string;
    status: string;
    created_at: string;
    warnings: string[];
    warning_details?: WarningDetail[];
    evidence_coverage?: EvidenceCoverage | null;
  }>;
};

type WarningDetail = {
  code: string;
  severity: "info" | "warning" | "degraded" | "error";
  category: string;
  label: string;
  description: string;
};

type EvidenceCoverage = {
  total: number;
  supported: number;
  partial: number;
  coverage_ratio: number;
  has_gaps: boolean;
  gap_labels: string[];
};

const LEGACY_WARNING_LABELS: Record<string, Pick<WarningDetail, "severity" | "label">> = {
  SYNTHETIC_DATA_SIGNAL: { severity: "info", label: "Document contains synthetic data" },
  INSUFFICIENT_EVIDENCE: { severity: "warning", label: "Insufficient evidence" },
  QUERY_PLANNER_PROVIDER_ERROR: { severity: "degraded", label: "Query planning degraded" },
  LLM_PROVIDER_ERROR: { severity: "degraded", label: "Model generation degraded" },
};

function detailsFor(run: Analytics["recent_runs"][number]): WarningDetail[] {
  if (run.warning_details) return run.warning_details;
  return run.warnings.map((code) => ({
    code,
    category: "legacy",
    description: "Run diagnostic signal. Deploy the latest backend to see the full description.",
    ...(LEGACY_WARNING_LABELS[code] ?? { severity: "warning" as const, label: "Run notice" }),
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
  if (!data) return <div className="loading">Loading analytics…</div>;
  const documentTotal = Object.values(data.documents).reduce((sum, value) => sum + value, 0);
  const completionRate = data.runs.total ? Math.round(data.runs.completed / data.runs.total * 100) : 0;
  return <section className="page"><header className="page-header"><div><span className="eyebrow">OPERATIONS & QUALITY</span><h1>Run Analytics</h1><p>Monitor answer quality, latency, knowledge gaps, and review status using live run data.</p></div></header>
    <div className="metric-grid">
      <article><span>Available documents</span><strong>{documentTotal}</strong><small>{Object.entries(data.documents).map(([key, value]) => `${statusLabel(key)} ${value}`).join(" · ") || "None"}</small></article>
      <article><span>Answer completion rate</span><strong>{completionRate}%</strong><small>{data.runs.completed}/{data.runs.total} completed · {data.runs.degraded ?? 0} automatic fallbacks</small></article>
      <article><span>Average end-to-end latency</span><strong>{Math.round(data.runs.avg_latency_ms)} ms</strong><small>Queue to completion</small></article>
      <article><span>Insufficient evidence</span><strong>{data.runs.no_evidence}</strong><small>Safe refusals</small></article>
      <article><span>Pending reviews</span><strong>{(data.reviews.pending ?? 0) + (data.reviews.in_review ?? 0)}</strong><small>Low-confidence charts</small></article>
      <article><span>Positive feedback</span><strong>{data.feedback.positive_rate === null ? "—" : `${Math.round(data.feedback.positive_rate * 100)}%`}</strong><small>{data.feedback.total} feedback items</small></article>
    </div>
    <div className="section-title"><h2>Recent Q&A Runs</h2><span>{data.recent_runs.length} {data.recent_runs.length === 1 ? "run" : "runs"}</span></div>
    <div className="run-list">{data.recent_runs.map((run) => {
      const warningDetails = detailsFor(run);
      return <article key={run.id}>
        <div className="run-summary"><strong>{run.question || run.title}</strong><small>{new Date(run.created_at).toLocaleString("en-US")}</small></div>
        <span className={`status ${run.status === "completed" ? "ready" : run.status}`}>{statusLabel(run.status)}</span>
        {warningDetails.length > 0 && <div className="run-signals" aria-label="Run notices">
          {warningDetails.map((warning) => <span
            className={`run-signal ${warning.severity}`}
            key={warning.code}
            title={`${warning.description} (${warning.code})`}
          >{warning.label}</span>)}
        </div>}
        {run.evidence_coverage?.has_gaps && <div className="run-coverage-detail" aria-label="Source support details">
          <strong>{run.evidence_coverage.supported} of {run.evidence_coverage.total} answer points supported</strong>
          <small>Not found in current sources: {run.evidence_coverage.gap_labels.join(" · ")}</small>
        </div>}
      </article>;
    })}{!data.recent_runs.length && <div className="empty">Run history will appear here after your first completed Q&A.</div>}</div>
  </section>;
}
