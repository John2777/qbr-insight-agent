import { execFileSync } from "node:child_process";
import { resolve } from "node:path";
import { expect, test } from "@playwright/test";

const projectRoot = resolve(import.meta.dirname, "../../..");

type SkillStatus = { name: string; kind: string; loaded: boolean };

async function skillStatus(page: import("@playwright/test").Page): Promise<SkillStatus[]> {
  return page.evaluate(async () => {
    const response = await fetch("/health/ready");
    if (!response.ok) throw new Error(`Health request failed: ${response.status}`);
    return (await response.json()).skills;
  });
}

function createSyntheticPresentation(outputPath: string) {
  const fixtureScript = resolve(projectRoot, "skills/extract-ppt-chart-data/scripts/self_test.py");
  execFileSync(
    resolve(projectRoot, ".venv/bin/python"),
    [
      "-c",
      [
        "import importlib.util, sys",
        "from pathlib import Path",
        "script, output = Path(sys.argv[1]), Path(sys.argv[2])",
        "sys.path.insert(0, str(script.parent))",
        "spec = importlib.util.spec_from_file_location('qbr_e2e_fixture', script)",
        "assert spec and spec.loader",
        "module = importlib.util.module_from_spec(spec)",
        "sys.modules[spec.name] = module",
        "spec.loader.exec_module(module)",
        "module.make_pptx(output)"
      ].join("; "),
      fixtureScript,
      outputPath
    ],
    { cwd: projectRoot }
  );
}

test("real browser uploads, answers with citations, and purges one presentation", async ({ page }, testInfo) => {
  test.setTimeout(120_000);
  const fixturePath = testInfo.outputPath("synthetic-qbr.pptx");
  createSyntheticPresentation(fixturePath);

  await page.goto("/documents");
  const initialSkills = await skillStatus(page);
  expect(initialSkills.find((skill) => skill.kind === "ParserSkill")?.loaded).toBe(false);
  expect(initialSkills.find((skill) => skill.kind === "ReasoningSkill")?.loaded).toBe(false);

  const uploadResponsePromise = page.waitForResponse(
    (response) => response.url().includes("/api/v1/documents") && response.request().method() === "POST"
  );
  await page.locator('input[type="file"]').setInputFiles(fixturePath);
  const uploadResponse = await uploadResponsePromise;
  expect(uploadResponse.status()).toBe(202);
  const uploaded = await uploadResponse.json() as { document: { id: string } };

  const documentCard = page.locator(`a[href="/documents/${uploaded.document.id}"]`);
  await expect(documentCard).toBeVisible();
  await expect(documentCard.getByText(/Ready|Partially available/)).toBeVisible({ timeout: 60_000 });

  const ingestedSkills = await skillStatus(page);
  expect(ingestedSkills.find((skill) => skill.kind === "ParserSkill")?.loaded).toBe(true);
  expect(ingestedSkills.find((skill) => skill.kind === "ReasoningSkill")?.loaded).toBe(false);

  await page.goto(`/chat?document=${uploaded.document.id}`);
  const question = page.getByLabel("Question");
  await expect(question).toBeEnabled();
  await question.fill("What was Q2 revenue?");
  await page.getByRole("button", { name: "Send" }).click();

  const answer = page.locator(".assistant-answer");
  await expect(answer).toContainText("20", { timeout: 60_000 });
  const citation = answer.getByRole("button", { name: "[1]" }).first();
  await expect(citation).toBeVisible();
  await citation.click();
  await expect(page.locator(".evidence-pane")).toContainText("Slide 1");

  await page.goto(`/documents/${uploaded.document.id}`);
  page.once("dialog", async (dialog) => {
    expect(dialog.message()).toContain("cannot be undone");
    await dialog.accept();
  });
  const purgeResponsePromise = page.waitForResponse(
    (response) => response.url().includes(`/api/v1/documents/${uploaded.document.id}/purge`)
      && response.request().method() === "DELETE"
  );
  await page.getByRole("button", { name: "Permanently delete" }).click();
  expect((await purgeResponsePromise).status()).toBe(200);
  await expect(page).toHaveURL(/\/documents$/);
  await expect(page.locator(`a[href="/documents/${uploaded.document.id}"]`)).toHaveCount(0);
});
