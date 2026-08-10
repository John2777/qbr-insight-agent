import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

const { apiMock, streamSseMock } = vi.hoisted(() => ({
  apiMock: vi.fn(),
  streamSseMock: vi.fn(),
}));

vi.mock("../api", () => ({ api: apiMock, streamSse: streamSseMock }));
vi.mock("../components/AssistantAnswer", () => ({
  AssistantAnswer: ({ content }: { content: string }) => <div>{content}</div>,
}));

import { ChatPage } from "./ChatPage";

afterEach(() => {
  vi.clearAllMocks();
});

describe("ChatPage", () => {
  it("replaces the empty state immediately and keeps the latest answer in view", async () => {
    let completed = false;
    let resolveMessagePost!: (value: { run_id: string; events_url: string }) => void;
    const messagePost = new Promise<{ run_id: string; events_url: string }>((resolve) => {
      resolveMessagePost = resolve;
    });
    const emptyConversation = {
      id: "conv_1",
      title: "Potential issues",
      scope: { document_ids: ["doc_1"] },
      messages: [],
    };
    const completedConversation = {
      ...emptyConversation,
      messages: [
        { id: "msg_user", role: "user", content: "What potential issues are in the current document?", status: "completed", citations: [] },
        { id: "msg_answer", role: "assistant", content: "Focus on execution variance.", status: "completed", citations: [] },
      ],
    };
    apiMock.mockImplementation((path: string, init?: RequestInit) => {
      if (path === "/api/v1/documents") {
        return Promise.resolve({ items: [{ id: "doc_1", title: "Growth QBR", status: "ready", updated_at: "now" }] });
      }
      if (path.endsWith("/messages") && init?.method === "POST") return messagePost;
      if (path === "/api/v1/conversations/conv_1") {
        return Promise.resolve(completed ? completedConversation : emptyConversation);
      }
      throw new Error(`Unexpected API call: ${path}`);
    });
    streamSseMock.mockImplementation(async (_path: string, onEvent: (event: { event: string; data: Record<string, unknown> }) => void) => {
      onEvent({ event: "status", data: { message: "Searching evidence" } });
      onEvent({ event: "answer_delta", data: { delta: "Focus on execution variance." } });
      completed = true;
    });
    const scrollToMock = vi.fn();
    Object.defineProperty(HTMLElement.prototype, "scrollTo", { configurable: true, value: scrollToMock });

    render(
      <MemoryRouter initialEntries={["/chat/conv_1"]}>
        <Routes><Route path="/chat/:conversationId" element={<ChatPage />}/></Routes>
      </MemoryRouter>,
    );

    const input = await screen.findByRole("textbox", { name: "Question" });
    await waitFor(() => expect(input).toBeEnabled());
    fireEvent.change(input, { target: { value: "What potential issues are in the current document?" } });
    fireEvent.submit(input.closest("form")!);

    expect(screen.getByText("What potential issues are in the current document?")).toBeInTheDocument();
    expect(screen.getByText("Submitting question…")).toBeInTheDocument();
    expect(screen.queryByText("Start with verifiable evidence")).not.toBeInTheDocument();
    await waitFor(() => expect(scrollToMock).toHaveBeenCalled());

    resolveMessagePost({ run_id: "run_1", events_url: "/events/run_1" });
    await waitFor(() => expect(screen.getByText("Focus on execution variance.")).toBeInTheDocument());
    expect(screen.queryByText("Start with verifiable evidence")).not.toBeInTheDocument();
    expect(scrollToMock.mock.calls.at(-1)?.[0]).toMatchObject({ top: expect.any(Number) });
  });
});
