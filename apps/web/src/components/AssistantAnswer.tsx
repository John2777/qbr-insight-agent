import { Fragment, useEffect, useMemo, useState } from "react";
import {
  BarElement,
  CategoryScale,
  Chart as ChartJS,
  Legend,
  LinearScale,
  LineElement,
  PointElement,
  Title,
  Tooltip,
  type ChartData,
  type ChartOptions
} from "chart.js";
import { Bar, Line } from "react-chartjs-2";
import { api } from "../api";
import type { Chart, Citation, ElementItem, SlideDetail } from "../types";

ChartJS.register(CategoryScale, LinearScale, BarElement, LineElement, PointElement, Title, Tooltip, Legend);

const COLORS = ["#335f8a", "#78905a", "#d38a45", "#765a9b", "#4b8f8c", "#b85c68"];

type AnswerBlock =
  | { kind: "heading"; text: string }
  | { kind: "paragraph"; text: string }
  | { kind: "list"; items: string[] }
  | { kind: "table"; rows: string[][] };

export function parseAnswer(content: string): AnswerBlock[] {
  const lines = content.split("\n");
  const blocks: AnswerBlock[] = [];
  for (let index = 0; index < lines.length;) {
    const line = lines[index].trim();
    if (!line) { index += 1; continue; }
    if (/^#{2,3}\s+/.test(line)) {
      blocks.push({ kind: "heading", text: line.replace(/^#{2,3}\s+/, "") });
      index += 1;
      continue;
    }
    if (line.startsWith("|")) {
      const rows: string[][] = [];
      while (index < lines.length && lines[index].trim().startsWith("|")) {
        const cells = lines[index].trim().slice(1, -1).split("|").map((cell) => cell.trim());
        if (!cells.every((cell) => /^:?-{3,}:?$/.test(cell))) rows.push(cells);
        index += 1;
      }
      if (rows.length) blocks.push({ kind: "table", rows });
      continue;
    }
    if (/^[-*]\s+/.test(line)) {
      const items: string[] = [];
      while (index < lines.length && /^[-*]\s+/.test(lines[index].trim())) {
        items.push(lines[index].trim().replace(/^[-*]\s+/, ""));
        index += 1;
      }
      blocks.push({ kind: "list", items });
      continue;
    }
    const paragraph = [line];
    index += 1;
    while (index < lines.length && lines[index].trim() && !/^(#{2,3}\s+|[-*]\s+|\|)/.test(lines[index].trim())) {
      paragraph.push(lines[index].trim());
      index += 1;
    }
    blocks.push({ kind: "paragraph", text: paragraph.join(" ") });
  }
  return blocks;
}

function InlineText({ text, citations, onCitation }: {
  text: string;
  citations: Citation[];
  onCitation: (citation: Citation) => void;
}) {
  const parts = text.split(/(\*\*[^*]+\*\*|\[\d+\])/g).filter(Boolean);
  return <>{parts.map((part, index) => {
    if (part.startsWith("**") && part.endsWith("**")) return <strong key={index}>{part.slice(2, -2)}</strong>;
    const citation = citations.find((item) => item.label === part);
    if (citation) {
      return <button className="inline-citation" onClick={() => onCitation(citation)} key={index}>{part}</button>;
    }
    return <Fragment key={index}>{part}</Fragment>;
  })}</>;
}

function StructuredText({ content, citations, onCitation }: {
  content: string;
  citations: Citation[];
  onCitation: (citation: Citation) => void;
}) {
  const legacy = content.startsWith("从整份文档看，业绩概况可依据以下关键内容归纳：");
  if (legacy) {
    return <div className="answer-sections">
      <section className="answer-overview"><h3>总体判断</h3><p>已综合执行摘要、财务数据和经营指标。关键数值与趋势已整理为下方表格和图表。</p></section>
      <details className="legacy-answer"><summary>查看原始证据摘录</summary><div>{content}</div></details>
    </div>;
  }
  return <div className="answer-sections">{parseAnswer(content).map((block, index) => {
    if (block.kind === "heading") return <h3 key={index}>{block.text}</h3>;
    if (block.kind === "paragraph") return <p key={index}><InlineText text={block.text} citations={citations} onCitation={onCitation}/></p>;
    if (block.kind === "list") return <ul key={index}>{block.items.map((item, itemIndex) => <li key={itemIndex}><InlineText text={item} citations={citations} onCitation={onCitation}/></li>)}</ul>;
    return <div className="answer-table-wrap" key={index}><table className="answer-table"><thead><tr>{block.rows[0].map((cell, cellIndex) => <th key={cellIndex}><InlineText text={cell} citations={citations} onCitation={onCitation}/></th>)}</tr></thead><tbody>{block.rows.slice(1).map((row, rowIndex) => <tr key={rowIndex}>{row.map((cell, cellIndex) => <td key={cellIndex}><InlineText text={cell} citations={citations} onCitation={onCitation}/></td>)}</tr>)}</tbody></table></div>;
  })}</div>;
}

function tableRows(element: ElementItem): string[][] | null {
  const rows = element.structured.rows;
  if (!Array.isArray(rows) || !rows.every((row) => Array.isArray(row))) return null;
  return rows.map((row) => row.map((cell) => String(cell)));
}

function chartScore(chart: Chart): number {
  const label = `${chart.title ?? ""} ${chart.series.map((series) => series.name ?? "").join(" ")}`.toLowerCase();
  const relevance = ["核心", "财务", "价值", "收入", "营收", "利润", "revenue", "profit", "value"]
    .filter((term) => label.includes(term)).length * 10;
  return relevance + Math.min(chart.series.length, 6) + Math.min(chart.series[0]?.points.length ?? 0, 12);
}

function AnswerChart({ chart, citation, onCitation }: {
  chart: Chart;
  citation?: Citation;
  onCitation: (citation: Citation) => void;
}) {
  const labels = chart.series[0]?.points.map((point, index) => point.category ?? String(index + 1)) ?? [];
  const hasPercentage = chart.series.some((series) => series.points.some((point) => point.display_value?.includes("%")));
  const datasets = chart.series.slice(0, 6).map((series, index) => {
    const percentage = series.points.some((point) => point.display_value?.includes("%"));
    return {
      label: series.name || `系列 ${index + 1}`,
      data: labels.map((label) => series.points.find((point) => point.category === label)?.y_value ?? null),
      borderColor: COLORS[index % COLORS.length],
      backgroundColor: `${COLORS[index % COLORS.length]}cc`,
      borderWidth: 2,
      pointRadius: 3,
      tension: 0.25,
      yAxisID: percentage ? "percentage" : "value"
    };
  });
  const data: ChartData<"bar" | "line", (number | null)[], string> = { labels, datasets };
  const options: ChartOptions<"bar" | "line"> = {
    responsive: true,
    maintainAspectRatio: false,
    interaction: { mode: "index", intersect: false },
    plugins: {
      legend: { position: "bottom", labels: { boxWidth: 10, usePointStyle: true } },
      tooltip: { callbacks: { label: (context) => `${context.dataset.label}: ${context.formattedValue}` } }
    },
    scales: {
      value: { beginAtZero: true, grid: { color: "#e9edf2" } },
      ...(hasPercentage ? { percentage: { beginAtZero: true, position: "right" as const, grid: { drawOnChartArea: false }, ticks: { callback: (value: string | number) => `${Number(value) * 100}%` } } } : {})
    }
  };
  const useLine = chart.chart_types.some((type) => type.toLowerCase().includes("line")) || labels.length > 6;
  return <section className="answer-visual-card">
    <header><div><span>趋势图</span><h4>{chart.title || "关键指标趋势"}</h4></div>{citation && <button onClick={() => onCitation(citation)}>{citation.label} 第 {citation.slide_no} 页</button>}</header>
    <div className="answer-chart">{useLine ? <Line data={data as ChartData<"line", (number | null)[], string>} options={options as ChartOptions<"line">}/> : <Bar data={data as ChartData<"bar", (number | null)[], string>} options={options as ChartOptions<"bar">}/>}</div>
  </section>;
}

export function AssistantAnswer({ content, citations, onCitation, showVisuals = true }: {
  content: string;
  citations: Citation[];
  onCitation: (citation: Citation) => void;
  showVisuals?: boolean;
}) {
  const [slides, setSlides] = useState<SlideDetail[]>([]);
  useEffect(() => {
    let active = true;
    const ids = showVisuals ? [...new Set(citations.map((citation) => citation.slide_id))] : [];
    if (!ids.length) { setSlides([]); return () => { active = false; }; }
    Promise.all(ids.map((id) => api<SlideDetail>(`/api/v1/slides/${id}`)))
      .then((items) => { if (active) setSlides(items); })
      .catch(() => { if (active) setSlides([]); });
    return () => { active = false; };
  }, [citations, showVisuals]);

  const charts = useMemo(() => slides.flatMap((slide) => slide.charts.map((chart) => ({ chart, slide })))
    .filter(({ chart }) => chart.series.some((series) => series.points.some((point) => point.y_value != null)))
    .sort((left, right) => chartScore(right.chart) - chartScore(left.chart)).slice(0, 1), [slides]);
  const tables = useMemo(() => slides.flatMap((slide) => slide.elements.map((element) => ({ rows: tableRows(element), slide })))
    .filter((item): item is { rows: string[][]; slide: SlideDetail } => Boolean(item.rows?.length))
    .sort((left, right) => {
      const leftText = left.rows[0].join(" "); const rightText = right.rows[0].join(" ");
      return Number(/核心|财务|指标/.test(rightText)) - Number(/核心|财务|指标/.test(leftText));
    }).slice(0, 1), [slides]);

  return <div className="assistant-answer">
    <StructuredText content={content} citations={citations} onCitation={onCitation}/>
    {showVisuals && (tables.length > 0 || charts.length > 0) && <div className="answer-visuals">
      {tables.map(({ rows, slide }) => {
        const source = citations.find((item) => item.slide_id === slide.id);
        return <section className="answer-visual-card" key={`table-${slide.id}`}>
          <header><div><span>数据表</span><h4>{slide.title || "关键指标"}</h4></div>{source && <button onClick={() => onCitation(source)}>{source.label} 第 {source.slide_no} 页</button>}</header>
          <div className="native-table-wrap"><table className="native-table"><thead><tr>{rows[0].map((cell, index) => <th key={index}>{cell}</th>)}</tr></thead><tbody>{rows.slice(1, 11).map((row, rowIndex) => <tr key={rowIndex}>{row.map((cell, cellIndex) => <td key={cellIndex}>{cell}</td>)}</tr>)}</tbody></table></div>
        </section>;
      })}
      {charts.map(({ chart, slide }) => <AnswerChart chart={chart} citation={citations.find((item) => item.slide_id === slide.id)} onCitation={onCitation} key={chart.id}/>)}
    </div>}
  </div>;
}
