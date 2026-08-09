import { render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
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
});
