export function ConfidenceBadge({ confidence, source }: { confidence: number; source?: string }) {
  const level = confidence >= 0.85 ? "high" : confidence >= 0.65 ? "medium" : "low";
  const sourceLabel = source === "embedded_workbook" ? "Chart source data" : source === "chart_cache_or_literal" ? "Chart cache" : source ?? "Native content";
  return <span className={`confidence ${level}`}>{sourceLabel} · {Math.round(confidence * 100)}%</span>;
}
