function authHeaders(): Record<string, string> {
  const token = window.localStorage.getItem("qbr_api_token");
  return token
    ? { Authorization: `Bearer ${token}` }
    : { "X-Workspace-ID": "ws_demo", "X-User-ID": "user_demo" };
}

export class ApiError extends Error {
  constructor(public status: number, public code: string, message: string) {
    super(message);
  }
}

export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(path, {
    ...init,
    credentials: "same-origin",
    headers: { ...authHeaders(), ...(init.headers ?? {}) }
  });
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    if (response.status === 401) window.dispatchEvent(new Event("qbr-auth-required"));
    throw new ApiError(response.status, body.code ?? "REQUEST_FAILED", body.detail ?? response.statusText);
  }
  if (response.status === 204) return undefined as T;
  return response.json() as Promise<T>;
}

export type SseEvent = { id: number; event: string; data: Record<string, unknown> };

export async function streamSse(path: string, onEvent: (event: SseEvent) => void): Promise<void> {
  const response = await fetch(path, { headers: authHeaders(), credentials: "same-origin" });
  if (!response.ok || !response.body) throw new ApiError(response.status, "STREAM_FAILED", response.statusText);
  const reader = response.body.pipeThrough(new TextDecoderStream()).getReader();
  let buffer = "";
  while (true) {
    const { value, done } = await reader.read();
    buffer += value ?? "";
    const frames = buffer.split("\n\n");
    buffer = frames.pop() ?? "";
    for (const frame of frames) {
      const lines = frame.split("\n");
      const event = lines.find((line) => line.startsWith("event:"))?.slice(6).trim() ?? "message";
      const id = Number(lines.find((line) => line.startsWith("id:"))?.slice(3).trim() ?? 0);
      const payload = lines.filter((line) => line.startsWith("data:"))
        .map((line) => line.slice(5).trim()).join("\n");
      onEvent({ id, event, data: payload ? JSON.parse(payload) as Record<string, unknown> : {} });
    }
    if (done) break;
  }
}

export async function uploadPresentation(file: File): Promise<{ document: { id: string }; job: { id: string } }> {
  const data = new FormData();
  data.append("file", file);
  data.append("deduplication", "new_version");
  return api("/api/v1/documents", { method: "POST", body: data });
}

export function statusLabel(status: string): string {
  return ({
    processing: "Processing",
    pending: "Pending",
    running: "Processing",
    ready: "Ready",
    partial: "Partially available",
    failed: "Failed",
    completed: "Completed",
    cancelled: "Canceled",
    in_review: "In review",
    resolved: "Resolved",
    dismissed: "Dismissed"
  } as Record<string, string>)[status] ?? status;
}
