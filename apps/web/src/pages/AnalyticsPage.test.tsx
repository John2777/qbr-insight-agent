import { act, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { notifyConversationsChanged } from "../conversationEvents";
import { AnalyticsPage } from "./AnalyticsPage";

const baseAnalytics = {
  documents: { ready: 1 },
  runs: { total: 1, completed: 1, failed: 0, no_evidence: 0, avg_latency_ms: 120 },
  reviews: {},
  feedback: { total: 0, positive_rate: null },
};

function jsonResponse(body: unknown): Response {
  return new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } });
}

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("analytics refresh", () => {
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
