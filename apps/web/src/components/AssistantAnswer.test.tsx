import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("react-chartjs-2", () => ({
  Bar: ({ data }: { data: { datasets: Array<{ label: string }> } }) => <div data-testid="bar-chart">{data.datasets.map((item) => item.label).join(",")}</div>,
  Line: ({ data }: { data: { datasets: Array<{ label: string }> } }) => <div data-testid="line-chart">{data.datasets.map((item) => item.label).join(",")}</div>
}));

import { AssistantAnswer, parseAnswer } from "./AssistantAnswer";

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("parseAnswer", () => {
  it("turns a management summary into headings, a table, and action bullets", () => {
    const blocks = parseAnswer(`## 总体判断

核心指标整体上行。[1]

## 关键趋势

| 指标 | 起始期 | 最新期 |
|---|---:|---:|
| VONB | 2023=4034 | 2025=5516 |

## 建议关注

- 关注区域差异。[2]`);

    expect(blocks.map((block) => block.kind)).toEqual([
      "heading", "paragraph", "heading", "table", "heading", "list"
    ]);
    expect(blocks.find((block) => block.kind === "table")).toMatchObject({
      rows: [["指标", "起始期", "最新期"], ["VONB", "2023=4034", "2025=5516"]]
    });
  });

  it("does not load or render slide visuals for a term-definition answer", () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);

    render(<AssistantAnswer
      content="**VONB（Value of New Business，新业务价值）**：衡量新业务预计创造的未来价值。[1]"
      citations={[{
        id: "cit_1", label: "[1]", document_title: "Growth QBR", slide_id: "slide_2", slide_no: 2,
        quote: "VONB 新业务价值", bbox: { x: 0, y: 0, w: 1, h: 1 }, confidence: 1,
        source_kind: "native_ooxml", preview_url: "/preview/2"
      }]}
      showVisuals={false}
      onCitation={() => undefined}
    />);

    expect(screen.getByText(/衡量新业务预计创造的未来价值/)).toBeInTheDocument();
    expect(screen.queryByText("数据表")).not.toBeInTheDocument();
    expect(screen.queryByText("趋势图")).not.toBeInTheDocument();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("keeps a relevant cited chart when another cited slide fails to load", async () => {
    const slide = {
      id: "slide_chart", slide_no: 6, title: "Monthly execution indicators", summary: "", quality_score: 1,
      preview_url: "/preview/6", document_title: "Execution QBR", elements: [], notes_text: "",
      charts: [
        {
          id: "chart_generic", element_id: "element_generic", title: "Core financial value", source_kind: "native_ooxml",
          confidence: 1, chart_types: ["line"], warnings: [],
          series: [{ id: "series_generic", name: "Revenue", chart_type: "line", points: [{ id: "p1", category: "Q1", y_value: 10, confidence: 1 }] }]
        },
        {
          id: "chart_execution", element_id: "element_execution", title: "Execution quality", source_kind: "native_ooxml",
          confidence: 1, chart_types: ["line"], warnings: [],
          series: [{ id: "series_execution", name: "Persistency", chart_type: "line", points: [{ id: "p2", category: "Q1", y_value: 90, display_value: "90%", confidence: 1 }] }]
        }
      ]
    };
    const fetchMock = vi.fn((path: string) => {
      if (path.endsWith("slide_missing")) return Promise.reject(new Error("network failure"));
      return Promise.resolve({ ok: true, status: 200, json: async () => slide });
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<AssistantAnswer
      content={"## 直接解释\n\n执行风险是目标落地偏离计划的可能性。[2]"}
      citations={[
        {
          id: "cit_1", label: "[1]", document_title: "Execution QBR", slide_id: "slide_missing", slide_no: 2,
          quote: "Risk gates", bbox: { x: 0, y: 0, w: 1, h: 1 }, confidence: 1,
          source_kind: "native_ooxml", preview_url: "/preview/2"
        },
        {
          id: "cit_2", label: "[2]", document_title: "Execution QBR", slide_id: "slide_chart", slide_no: 6,
          element_id: "element_execution", quote: "Execution quality — Persistency: Q1=90%", bbox: { x: 0, y: 0, w: 1, h: 1 }, confidence: 1,
          source_kind: "native_ooxml", preview_url: "/preview/6"
        }
      ]}
      onCitation={() => undefined}
    />);

    await waitFor(() => expect(screen.getByText("Execution quality")).toBeInTheDocument());
    expect(screen.getByText("趋势图")).toBeInTheDocument();
    expect(screen.getByTestId("line-chart")).toHaveTextContent("Persistency");
    expect(screen.getByTestId("line-chart")).not.toHaveTextContent("Revenue");
  });
});
