import { useEffect, useRef, useState } from "react";

type CopyState = "idle" | "copied" | "error";

async function writeToClipboard(text: string): Promise<void> {
  if (navigator.clipboard?.writeText) {
    try {
      await navigator.clipboard.writeText(text);
      return;
    } catch {
      // Fall through for browsers that expose Clipboard API but deny access.
    }
  }

  const textarea = document.createElement("textarea");
  textarea.value = text;
  textarea.setAttribute("readonly", "");
  textarea.style.position = "fixed";
  textarea.style.opacity = "0";
  document.body.appendChild(textarea);
  textarea.select();
  const copied = document.execCommand("copy");
  textarea.remove();
  if (!copied) throw new Error("Clipboard copy was rejected");
}

export function MessageCopyButton({ content, kind }: {
  content: string;
  kind: "question" | "answer";
}) {
  const [state, setState] = useState<CopyState>("idle");
  const resetTimer = useRef<number | undefined>(undefined);

  useEffect(() => () => window.clearTimeout(resetTimer.current), []);

  async function copy() {
    window.clearTimeout(resetTimer.current);
    try {
      await writeToClipboard(content);
      setState("copied");
    } catch {
      setState("error");
    }
    resetTimer.current = window.setTimeout(() => setState("idle"), 2000);
  }

  const label = state === "copied" ? "Copied" : state === "error" ? "Copy failed" : "Copy";
  const kindLabel = kind === "question" ? "question" : "answer";
  return <button
    type="button"
    className="message-copy-button"
    data-state={state}
    aria-label={state === "idle" ? `Copy ${kindLabel}` : state === "copied" ? `${kindLabel[0].toUpperCase()}${kindLabel.slice(1)} copied` : `${kindLabel[0].toUpperCase()}${kindLabel.slice(1)} copy failed`}
    onClick={() => void copy()}
  >
    <svg aria-hidden="true" viewBox="0 0 20 20">
      {state === "copied"
        ? <path d="m4.5 10.5 3.2 3.2 7.8-8"/>
        : <><rect x="6" y="6" width="9" height="10" rx="1.5"/><path d="M5 13H4a1.5 1.5 0 0 1-1.5-1.5v-7A1.5 1.5 0 0 1 4 3h7A1.5 1.5 0 0 1 12.5 4.5V5"/></>}
    </svg>
    <span aria-live="polite">{label}</span>
  </button>;
}
