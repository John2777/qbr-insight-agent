import { describe, expect, it } from "vitest";
import { statusLabel } from "./api";

describe("statusLabel", () => {
  it("uses human-readable Chinese labels and preserves unknown states", () => {
    expect(statusLabel("ready")).toBe("Ready");
    expect(statusLabel("custom")).toBe("custom");
  });
});
