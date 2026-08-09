import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { MessageCopyButton } from "./MessageCopyButton";

describe("MessageCopyButton", () => {
  it("copies the complete message and confirms success", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText }
    });

    render(<MessageCopyButton content={"完整回答\n包含第二行 [1]"} kind="回答"/>);
    fireEvent.click(screen.getByRole("button", { name: "复制回答" }));

    await waitFor(() => expect(writeText).toHaveBeenCalledWith("完整回答\n包含第二行 [1]"));
    expect(screen.getByText("已复制")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "回答已复制" })).toBeInTheDocument();
  });
});
