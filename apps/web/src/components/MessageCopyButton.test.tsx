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

    render(<MessageCopyButton content={"Complete answer\nwith a second line [1]"} kind="answer"/>);
    fireEvent.click(screen.getByRole("button", { name: "Copy answer" }));

    await waitFor(() => expect(writeText).toHaveBeenCalledWith("Complete answer\nwith a second line [1]"));
    expect(screen.getByText("Copied")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Answer copied" })).toBeInTheDocument();
  });
});
