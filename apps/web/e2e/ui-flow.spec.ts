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
  await expect(page.getByRole("heading", { name: "Quarterly Business Documents" })).toBeVisible();
  await expect(page.getByText("FY25 Q4 Review")).toBeVisible();
  await page.getByRole("link", { name: /FY25 Q4 Review/ }).focus();
  await expect(page.getByRole("link", { name: /FY25 Q4 Review/ })).toBeFocused();
});

test("queued answer is rendered from the SSE stream", async ({ page }) => {
  await mockSession(page);
  let complete = false;
  await page.route("**/api/v1/documents", (route) => route.fulfill({ json: { items: [{ id: "doc_1", title: "FY25 Q4 Review", status: "ready", updated_at: "2026-08-07T00:00:00Z" }] } }));
  await page.route("**/api/v1/conversations?limit=30", (route) => route.fulfill({ json: { items: complete ? [{ id: "conv_1", title: "Revenue", scope: { document_ids: ["doc_1"] }, message_count: 2, last_question: "What was Q2 revenue?", last_activity_at: "2026-08-09T12:00:00Z", created_at: "2026-08-09T12:00:00Z" }] : [] } }));
  await page.route("**/api/v1/conversations", (route) => route.fulfill({ status: 201, json: { id: "conv_1", title: "Revenue", scope: { document_ids: ["doc_1"] }, messages: [] } }));
  await page.route("**/api/v1/conversations/conv_1/messages", (route) => route.fulfill({ status: 202, json: { run_id: "run_1", events_url: "/api/v1/runs/run_1/events" } }));
  await page.route("**/api/v1/runs/run_1/events", async (route) => {
    complete = true;
    await route.fulfill({
      contentType: "text/event-stream",
      body: 'event: status\nid: 1\ndata: {"message":"Searching evidence"}\n\nevent: answer_delta\nid: 2\ndata: {"delta":"Q2 revenue was 20. [1]"}\n\nevent: completed\nid: 3\ndata: {}\n\n'
    });
  });
  await page.route("**/api/v1/conversations/conv_1", (route) => route.fulfill({ json: { id: "conv_1", title: "Revenue", scope: { document_ids: ["doc_1"] }, messages: complete ? [
    { id: "msg_user_1", role: "user", content: "What was Q2 revenue?", status: "completed", citations: [] },
    { id: "msg_1", role: "assistant", content: "Q2 revenue was 20. [1]", status: "completed", citations: [] }
  ] : [] } }));

  await page.goto("/chat?document=doc_1");
  await page.getByLabel("Question").fill("What was Q2 revenue?");
  await page.getByRole("button", { name: "Send" }).click();
  await expect(page.getByText("Q2 revenue was 20. [1]")).toBeVisible();
  await expect(page.getByRole("button", { name: "Copy question" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Copy answer" })).toBeVisible();
  await expect(page.getByRole("link", { name: /Revenue.*What was Q2 revenue/ })).toBeVisible();
});

test("conversation history opens the complete question and answer", async ({ page }) => {
  await mockSession(page);
  let historyVisible = true;
  await page.route("**/api/v1/documents", (route) => route.fulfill({ json: { items: [] } }));
  await page.route("**/api/v1/conversations?limit=30", (route) => route.fulfill({ json: { items: historyVisible ? [{
    id: "conv_history", title: "VONB meaning", scope: { document_ids: [] }, message_count: 2,
    last_question: "What does VONB mean?", last_activity_at: "2026-08-09T12:00:00Z", created_at: "2026-08-09T12:00:00Z"
  }] : [] } }));
  await page.route("**/api/v1/conversations/conv_history", async (route) => {
    if (route.request().method() === "DELETE") {
      historyVisible = false;
      await route.fulfill({ status: 204 });
      return;
    }
    await route.fulfill({ json: {
      id: "conv_history", title: "VONB meaning", scope: { document_ids: [] }, messages: [
        { id: "history_user", role: "user", content: "What does VONB mean?", status: "completed", citations: [] },
        { id: "history_answer", role: "assistant", content: "VONB means value of new business.", status: "completed", citations: [] }
      ]
    } });
  });

  await page.goto("/documents");
  await page.getByRole("link", { name: /VONB meaning.*What does VONB mean/ }).click();

  await expect(page).toHaveURL(/\/chat\/conv_history$/);
  await expect(page.getByRole("main").getByText("What does VONB mean?", { exact: true })).toBeVisible();
  await expect(page.getByRole("main").getByText("VONB means value of new business.", { exact: true })).toBeVisible();

  page.once("dialog", (dialog) => dialog.accept());
  await page.getByRole("button", { name: "Delete conversation: VONB meaning" }).click();
  await expect(page).toHaveURL(/\/chat$/);
  await expect(page.getByRole("link", { name: /VONB meaning/ })).toHaveCount(0);
});

test("analytics page exposes quality metrics", async ({ page }) => {
  await mockSession(page);
  await page.route("**/api/v1/analytics/summary", (route) => route.fulfill({ json: {
    documents: { ready: 3 }, runs: { total: 10, completed: 9, failed: 1, no_evidence: 2, avg_latency_ms: 842 },
    reviews: { pending: 1 }, feedback: { total: 4, positive_rate: 0.75 }, recent_runs: []
  } }));
  await page.goto("/analytics");
  await expect(page.getByRole("heading", { name: "Run Analytics" })).toBeVisible();
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
  await expect(page.getByRole("heading", { name: "Sign in to the demo workspace" })).toBeVisible();
  await page.getByLabel("Username").fill("interviewer");
  await page.getByLabel("Password").fill("correct horse battery staple");
  await page.getByRole("button", { name: "Secure sign in" }).click();
  await expect(page.getByRole("heading", { name: "Quarterly Business Documents" })).toBeVisible();
});
