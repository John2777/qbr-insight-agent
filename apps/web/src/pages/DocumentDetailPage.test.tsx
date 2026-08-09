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

const readyDocumentResponse = {
  ...documentResponse,
  status: "ready",
  versions: [{ id: "dv_1", active_parser_run_id: "pr_1" }]
};

const slideItems = [
  { id: "slide_1", slide_no: 1, title: "First", quality_score: 1, preview_url: "/preview/1", thumbnail_url: "/thumbnail/1" },
  { id: "slide_2", slide_no: 2, title: "Second", quality_score: 1, preview_url: "/preview/2", thumbnail_url: "/thumbnail/2" }
];

function slideDetail(index: 0 | 1) {
  return { ...slideItems[index], document_title: "FY25 QBR", elements: [], charts: [] };
}

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

describe("slide preview loading", () => {
  it("lazy-loads thumbnail assets and fetches a selected slide only once", async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const path = String(input);
      if (path === "/api/v1/documents/doc_1") return jsonResponse(readyDocumentResponse);
      if (path === "/api/v1/document-versions/dv_1/slides") return jsonResponse({ items: slideItems });
      if (path === "/api/v1/slides/slide_1") return jsonResponse(slideDetail(0));
      if (path === "/api/v1/slides/slide_2") return jsonResponse(slideDetail(1));
      throw new Error(`Unexpected request: ${path}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    renderPage();

    const secondThumbnail = await screen.findByAltText("第 2 页缩略图");
    expect(secondThumbnail).toHaveAttribute("src", "/thumbnail/2");
    expect(secondThumbnail).toHaveAttribute("loading", "lazy");
    expect(secondThumbnail).toHaveAttribute("decoding", "async");

    fireEvent.click(screen.getByText("Second").closest("button")!);

    await waitFor(() => {
      expect(screen.getByLabelText("第 2 页预览")).toBeInTheDocument();
    });
    fireEvent.click(screen.getByText("First").closest("button")!);
    await waitFor(() => expect(screen.getByLabelText("第 1 页预览")).toBeInTheDocument());
    fireEvent.click(screen.getByText("Second").closest("button")!);
    await waitFor(() => expect(screen.getByLabelText("第 2 页预览")).toBeInTheDocument());

    expect(fetchMock.mock.calls.filter(([path]) => path === "/api/v1/slides/slide_1")).toHaveLength(1);
    expect(fetchMock.mock.calls.filter(([path]) => path === "/api/v1/slides/slide_2")).toHaveLength(1);
  });
});
