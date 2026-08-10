import { useEffect, useState } from "react";
import { NavLink, useLocation, useNavigate } from "react-router-dom";
import { ApiError, api } from "../api";
import { CONVERSATIONS_CHANGED_EVENT, notifyConversationsChanged } from "../conversationEvents";
import type { ConversationSummary } from "../types";

function formatActivity(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  return date.toLocaleDateString("en-US", { month: "short", day: "numeric" });
}

export function ConversationHistory() {
  const location = useLocation();
  const navigate = useNavigate();
  const [items, setItems] = useState<ConversationSummary[]>([]);
  const [loaded, setLoaded] = useState(false);
  const [deletingId, setDeletingId] = useState<string | null>(null);
  const [deleteError, setDeleteError] = useState("");

  useEffect(() => {
    let active = true;
    const load = () => {
      void api<{ items: ConversationSummary[] }>("/api/v1/conversations?limit=30")
        .then((result) => { if (active) setItems(result.items); })
        .catch(() => undefined)
        .finally(() => { if (active) setLoaded(true); });
    };
    load();
    window.addEventListener(CONVERSATIONS_CHANGED_EVENT, load);
    return () => {
      active = false;
      window.removeEventListener(CONVERSATIONS_CHANGED_EVENT, load);
    };
  }, [location.pathname]);

  const deleteConversation = async (item: ConversationSummary) => {
    if (!window.confirm(`Delete the complete Q&A history for “${item.title}”? This action cannot be undone.`)) return;
    setDeletingId(item.id);
    setDeleteError("");
    try {
      await api<void>(`/api/v1/conversations/${item.id}`, { method: "DELETE" });
      setItems((current) => current.filter((conversation) => conversation.id !== item.id));
      notifyConversationsChanged();
      if (location.pathname === `/chat/${item.id}`) navigate("/chat", { replace: true });
    } catch (error) {
      setDeleteError(error instanceof ApiError && error.status === 409
        ? "This answer is still being generated and cannot be deleted yet."
        : "Delete failed. Try again later.");
    } finally {
      setDeletingId(null);
    }
  };

  return <section className="conversation-history" aria-labelledby="conversation-history-title">
    <header><h2 id="conversation-history-title">Q&A History</h2><span>Latest {items.length}</span></header>
    <div className="conversation-history-list">
      {items.map((item) => <div className="conversation-history-item" key={item.id}>
        <NavLink
          to={`/chat/${item.id}`}
          className={({ isActive }) => isActive ? "conversation-history-link active" : "conversation-history-link"}
          title={item.title}
        >
          <div><strong>{item.title}</strong><time dateTime={item.last_activity_at}>{formatActivity(item.last_activity_at)}</time></div>
          <small>{item.last_question || "No question yet"}</small>
        </NavLink>
        <button
          className={deletingId === item.id ? "conversation-history-delete deleting" : "conversation-history-delete"}
          type="button"
          aria-label={`Delete conversation: ${item.title}`}
          title="Delete this Q&A"
          disabled={deletingId !== null}
          onClick={() => void deleteConversation(item)}
        >
          {deletingId === item.id ? <span className="conversation-delete-spinner" aria-hidden="true"/> : <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 7h16M9 7V4h6v3m3 0-1 13H7L6 7m4 4v5m4-5v5"/></svg>}
        </button>
      </div>)}
      {deleteError && <p className="conversation-history-error" role="alert">{deleteError}</p>}
      {loaded && !items.length && <p className="conversation-history-empty">No Q&A history</p>}
      {!loaded && <p className="conversation-history-empty">Loading…</p>}
    </div>
  </section>;
}
