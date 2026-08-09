import { describe, expect, it } from "vitest";

import { MAX_UPLOAD_BYTES, validatePresentationFile } from "./DocumentsPage";

describe("document upload validation", () => {
  it("accepts a PPTX at the 10 MiB limit", () => {
    expect(validatePresentationFile({ name: "qbr.pptx", size: MAX_UPLOAD_BYTES })).toBe("");
  });

  it("rejects a PPTX above the 10 MiB limit", () => {
    expect(validatePresentationFile({ name: "qbr.pptx", size: MAX_UPLOAD_BYTES + 1 })).toBe("文件不能超过 10 MiB");
  });
});
