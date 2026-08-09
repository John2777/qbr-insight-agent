import { expect, test } from "@playwright/test";

async function mockSession(page: import("@playwright/test").Page) {
  await page.route("**/api/v1/auth/session", (route) => route.fulfill({ json: { workspace_id: "ws_demo", user_id: "user_demo", roles: ["admin"] } }));
}

test("document evidence workflow is keyboard accessible", async ({ page }) => {
  await mockSession(page);
  await page.route("**/api/v1/documents", async (route) => {
    if (route.request().method() === "GET") await route.fulfill({ json: { items: [{ id: "doc_1", title: "FY25 Q4 Review", status: "ready", active_version_id: "dv_1", slide_count: 2, review_count: 0, updated_at: "2026-08-07T00:00:00Z" }] } });
    else await route.fulfill({ status: 202, json: { document: { id: "doc_1" }, job: { id: "job_1" } } });
  });
  await page.goto("/documents");
  await expect(page.getByRole("heading", { name: "季度业务文档" })).toBeVisible();
  await expect(page.getByText("FY25 Q4 Review")).toBeVisible();
  await page.getByRole("link", { name: /FY25 Q4 Review/ }).focus();
  await expect(page.getByRole("link", { name: /FY25 Q4 Review/ })).toBeFocused();
});

test("queued answer is rendered from the SSE stream", async ({ page }) => {
  await mockSession(page);
  let complete = false;
  await page.route("**/api/v1/documents", (route) => route.fulfill({ json: { items: [{ id: "doc_1", title: "FY25 Q4 Review", status: "ready", updated_at: "2026-08-07T00:00:00Z" }] } }));
  await page.route("**/api/v1/conversations", (route) => route.fulfill({ status: 201, json: { id: "conv_1", title: "Revenue", scope: { document_ids: ["doc_1"] }, messages: [] } }));
  await page.route("**/api/v1/conversations/conv_1/messages", (route) => route.fulfill({ status: 202, json: { run_id: "run_1", events_url: "/api/v1/runs/run_1/events" } }));
  await page.route("**/api/v1/runs/run_1/events", async (route) => {
    complete = true;
    await route.fulfill({
      contentType: "text/event-stream",
      body: 'event: status\nid: 1\ndata: {"message":"正在检索"}\n\nevent: answer_delta\nid: 2\ndata: {"delta":"Q2 Revenue 为 20。 [1]"}\n\nevent: completed\nid: 3\ndata: {}\n\n'
    });
  });
  await page.route("**/api/v1/conversations/conv_1", (route) => route.fulfill({ json: { id: "conv_1", title: "Revenue", scope: { document_ids: ["doc_1"] }, messages: complete ? [
    { id: "msg_user_1", role: "user", content: "Q2 Revenue 是多少？", status: "completed", citations: [] },
    { id: "msg_1", role: "assistant", content: "Q2 Revenue 为 20。 [1]", status: "completed", citations: [] }
  ] : [] } }));

  await page.goto("/chat?document=doc_1");
  await page.getByLabel("问题").fill("Q2 Revenue 是多少？");
  await page.getByRole("button", { name: "发送" }).click();
  await expect(page.getByText("Q2 Revenue 为 20。 [1]")).toBeVisible();
  await expect(page.getByRole("button", { name: "复制提问" })).toBeVisible();
  await expect(page.getByRole("button", { name: "复制回答" })).toBeVisible();
});

test("analytics page exposes quality metrics", async ({ page }) => {
  await mockSession(page);
  await page.route("**/api/v1/analytics/summary", (route) => route.fulfill({ json: {
    documents: { ready: 3 }, runs: { total: 10, completed: 9, failed: 1, no_evidence: 2, avg_latency_ms: 842 },
    reviews: { pending: 1 }, feedback: { total: 4, positive_rate: 0.75 }, recent_runs: []
  } }));
  await page.goto("/analytics");
  await expect(page.getByRole("heading", { name: "运行分析" })).toBeVisible();
  await expect(page.getByText("90%" )).toBeVisible();
  await expect(page.getByText("842 ms")).toBeVisible();
});

test("anonymous user signs in before accessing the application", async ({ page }) => {
  await page.route("**/api/v1/auth/session", (route) => route.fulfill({ status: 401, json: { code: "AUTHENTICATION_REQUIRED", detail: "Sign in is required" } }));
  await page.route("**/api/v1/auth/login", async (route) => {
    const body = route.request().postDataJSON();
    expect(body).toEqual({ username: "interviewer", password: "correct horse battery staple" });
    await route.fulfill({ status: 200, json: { user_id: "user_demo", workspace_id: "ws_demo", role: "admin" } });
  });
  await page.route("**/api/v1/documents", (route) => route.fulfill({ json: { items: [] } }));
  await page.goto("/documents");
  await expect(page.getByRole("heading", { name: "登录演示空间" })).toBeVisible();
  await page.getByLabel("用户名").fill("interviewer");
  await page.getByLabel("密码").fill("correct horse battery staple");
  await page.getByRole("button", { name: "安全登录" }).click();
  await expect(page.getByRole("heading", { name: "季度业务文档" })).toBeVisible();
});
