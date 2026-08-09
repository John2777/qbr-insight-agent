import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { notifyConversationsChanged } from "../conversationEvents";
import { AnalyticsPage } from "./AnalyticsPage";

const baseAnalytics = {
  documents: { ready: 1 },
  runs: { total: 1, completed: 1, failed: 0, no_evidence: 0, degraded: 0, avg_latency_ms: 120 },
  reviews: {},
  feedback: { total: 0, positive_rate: null },
};

function jsonResponse(body: unknown): Response {
  return new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } });
}

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("analytics refresh", () => {
  it("renders provider fallbacks as degradation and synthetic data as information", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(jsonResponse({
      ...baseAnalytics,
      runs: { ...baseAnalytics.runs, degraded: 1 },
      recent_runs: [{
        id: "run_degraded",
        title: "公司的优势在哪里",
        status: "completed",
        created_at: "2026-08-09T12:00:00Z",
        warnings: ["QUERY_PLANNER_PROVIDER_ERROR", "SYNTHETIC_DATA_SIGNAL", "LLM_PROVIDER_ERROR"],
        warning_details: [
          { code: "QUERY_PLANNER_PROVIDER_ERROR", severity: "degraded", category: "provider_fallback", label: "查询规划已降级", description: "已使用确定性查询计划。" },
          { code: "SYNTHETIC_DATA_SIGNAL", severity: "info", category: "data_caveat", label: "文档含模拟数据", description: "这不是系统故障。" },
          { code: "LLM_PROVIDER_ERROR", severity: "degraded", category: "provider_fallback", label: "模型生成已降级", description: "已返回确定性答案。" },
        ],
      }],
    })));

    render(<AnalyticsPage />);

    expect(await screen.findByText("已完成")).toBeInTheDocument();
    expect(screen.getByText("查询规划已降级")).toHaveClass("degraded");
    expect(screen.getByText("文档含模拟数据")).toHaveClass("info");
    expect(screen.getByText("模型生成已降级")).toHaveClass("degraded");
    expect(screen.queryByText("QUERY_PLANNER_PROVIDER_ERROR")).not.toBeInTheDocument();
  });

  it("removes a deleted conversation's run when conversation data changes", async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(jsonResponse({
        ...baseAnalytics,
        recent_runs: [{ id: "run_1", title: "待删除问答", status: "completed", created_at: "2026-08-09T12:00:00Z", warnings: [] }],
      }))
      .mockResolvedValueOnce(jsonResponse({
        ...baseAnalytics,
        runs: { ...baseAnalytics.runs, total: 0, completed: 0 },
        recent_runs: [],
      }));
    vi.stubGlobal("fetch", fetchMock);
    render(<AnalyticsPage />);

    expect(await screen.findByText("待删除问答")).toBeInTheDocument();

    act(() => notifyConversationsChanged());

    await waitFor(() => expect(screen.queryByText("待删除问答")).not.toBeInTheDocument());
    expect(screen.getByText("完成第一条问答后，这里会出现运行记录。")).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });
});
