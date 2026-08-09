import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter, Route, Routes } from "react-router-dom";

import { DocumentDetailPage } from "./DocumentDetailPage";

const documentResponse = {
  id: "doc_1",
  title: "FY25 QBR",
  status: "processing",
  versions: [{ id: "dv_1" }]
};

function jsonResponse(body: unknown): Response {
  return new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } });
}

function renderPage() {
  return render(
    <MemoryRouter initialEntries={["/documents/doc_1"]}>
      <Routes>
        <Route path="/documents/:documentId" element={<DocumentDetailPage />} />
        <Route path="/documents" element={<div>文档列表</div>} />
      </Routes>
    </MemoryRouter>
  );
}

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("single document purge", () => {
  it("requires confirmation, calls the purge endpoint, and returns to the document list", async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(jsonResponse(documentResponse))
      .mockResolvedValueOnce(jsonResponse({ status: "completed" }));
    vi.stubGlobal("fetch", fetchMock);
    vi.spyOn(window, "confirm").mockReturnValue(true);
    renderPage();

    fireEvent.click(await screen.findByRole("button", { name: "彻底删除" }));

    expect(window.confirm).toHaveBeenCalledWith(expect.stringContaining("此操作不可恢复"));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
    expect(fetchMock.mock.calls[1][0]).toBe("/api/v1/documents/doc_1/purge");
    expect(fetchMock.mock.calls[1][1]).toMatchObject({ method: "DELETE" });
    expect(await screen.findByText("文档列表")).toBeInTheDocument();
  });

  it("does not call the endpoint when confirmation is cancelled", async () => {
    const fetchMock = vi.fn().mockResolvedValueOnce(jsonResponse(documentResponse));
    vi.stubGlobal("fetch", fetchMock);
    vi.spyOn(window, "confirm").mockReturnValue(false);
    renderPage();

    fireEvent.click(await screen.findByRole("button", { name: "彻底删除" }));

    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
});
