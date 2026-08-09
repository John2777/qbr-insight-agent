import { describe, expect, it } from "vitest";
import { parseAnswer } from "./AssistantAnswer";

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
});
