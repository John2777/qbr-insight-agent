export function ConfidenceBadge({ confidence, source }: { confidence: number; source?: string }) {
  const level = confidence >= 0.85 ? "high" : confidence >= 0.65 ? "medium" : "low";
  const sourceLabel = source === "embedded_workbook" ? "图表源数据" : source === "chart_cache_or_literal" ? "图表缓存" : source ?? "原生内容";
  return <span className={`confidence ${level}`}>{sourceLabel} · {Math.round(confidence * 100)}%</span>;
}

