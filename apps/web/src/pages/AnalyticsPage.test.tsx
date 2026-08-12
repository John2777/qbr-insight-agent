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
        title: "Quarterly performance",
        question: "What are the company's strengths?",
        status: "completed",
        created_at: "2026-08-09T12:00:00Z",
        warnings: ["QUERY_PLANNER_PROVIDER_ERROR", "SYNTHETIC_DATA_SIGNAL", "LLM_PROVIDER_ERROR"],
        warning_details: [
          { code: "QUERY_PLANNER_PROVIDER_ERROR", severity: "degraded", category: "provider_fallback", label: "Query planning degraded", description: "A deterministic query plan was used." },
          { code: "SYNTHETIC_DATA_SIGNAL", severity: "info", category: "data_caveat", label: "Document contains synthetic data", description: "This is not a system failure." },
          { code: "LLM_PROVIDER_ERROR", severity: "degraded", category: "provider_fallback", label: "Model generation degraded", description: "A deterministic answer was returned." },
        ],
      }],
    })));

    render(<AnalyticsPage />);

    expect(await screen.findByText("Completed")).toBeInTheDocument();
    expect(screen.getByText("What are the company's strengths?")).toBeInTheDocument();
    expect(screen.queryByText("Quarterly performance")).not.toBeInTheDocument();
    expect(screen.getByText("Query planning degraded")).toHaveClass("degraded");
    expect(screen.getByText("Document contains synthetic data")).toHaveClass("info");
    expect(screen.getByText("Model generation degraded")).toHaveClass("degraded");
    expect(screen.queryByText("QUERY_PLANNER_PROVIDER_ERROR")).not.toBeInTheDocument();
  });

  it("removes a deleted conversation's run when conversation data changes", async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(jsonResponse({
        ...baseAnalytics,
        recent_runs: [{ id: "run_1", title: "Q&A to delete", status: "completed", created_at: "2026-08-09T12:00:00Z", warnings: [] }],
      }))
      .mockResolvedValueOnce(jsonResponse({
        ...baseAnalytics,
        runs: { ...baseAnalytics.runs, total: 0, completed: 0 },
        recent_runs: [],
      }));
    vi.stubGlobal("fetch", fetchMock);
    render(<AnalyticsPage />);

    expect(await screen.findByText("Q&A to delete")).toBeInTheDocument();

    act(() => notifyConversationsChanged());

    await waitFor(() => expect(screen.queryByText("Q&A to delete")).not.toBeInTheDocument());
    expect(screen.getByText("Run history will appear here after your first completed Q&A.")).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("uses an English partial-support notice without exposing coverage details", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(jsonResponse({
      ...baseAnalytics,
      recent_runs: [{
        id: "run_partial",
        title: "Growth and drivers",
        status: "completed",
        created_at: "2026-08-10T12:51:13Z",
        warnings: ["PARTIAL_EVIDENCE_COVERAGE"],
        warning_details: [{
          code: "PARTIAL_EVIDENCE_COVERAGE",
          severity: "warning",
          category: "evidence_quality",
          label: "部分内容暂无资料支持",
          description: "回答中的其余内容仍有资料支持；当前文档暂未提供问题中部分内容所需的信息。",
        }],
        evidence_coverage: {
          total: 4,
          supported: 3,
          partial: 1,
          coverage_ratio: 0.75,
          has_gaps: true,
          gap_labels: ["Which business segments drove growth"],
        },
      }],
    })));

    render(<AnalyticsPage />);

    expect(await screen.findByText("Some details lack source support")).toHaveClass("warning");
    expect(screen.queryByText("部分内容暂无资料支持")).not.toBeInTheDocument();
    expect(screen.queryByText("3 of 4 answer points supported")).not.toBeInTheDocument();
    expect(screen.queryByText("Not found in current sources: Which business segments drove growth")).not.toBeInTheDocument();
  });
});
