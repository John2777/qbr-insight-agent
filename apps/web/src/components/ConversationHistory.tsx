import { useEffect, useState } from "react";
import { NavLink, useLocation } from "react-router-dom";
import { api } from "../api";
import type { ConversationSummary } from "../types";

const HISTORY_CHANGED_EVENT = "qbr-conversations-changed";

export function notifyConversationHistoryChanged() {
  window.dispatchEvent(new Event(HISTORY_CHANGED_EVENT));
}

function formatActivity(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  return date.toLocaleDateString("zh-CN", { month: "numeric", day: "numeric" });
}

export function ConversationHistory() {
  const location = useLocation();
  const [items, setItems] = useState<ConversationSummary[]>([]);
  const [loaded, setLoaded] = useState(false);

  useEffect(() => {
    let active = true;
    const load = () => {
      void api<{ items: ConversationSummary[] }>("/api/v1/conversations?limit=30")
        .then((result) => { if (active) setItems(result.items); })
        .catch(() => undefined)
        .finally(() => { if (active) setLoaded(true); });
    };
    load();
    window.addEventListener(HISTORY_CHANGED_EVENT, load);
    return () => {
      active = false;
      window.removeEventListener(HISTORY_CHANGED_EVENT, load);
    };
  }, [location.pathname]);

  return <section className="conversation-history" aria-labelledby="conversation-history-title">
    <header><h2 id="conversation-history-title">历史问答</h2><span>最近 {items.length} 条</span></header>
    <div className="conversation-history-list">
      {items.map((item) => <NavLink
        key={item.id}
        to={`/chat/${item.id}`}
        className={({ isActive }) => isActive ? "conversation-history-link active" : "conversation-history-link"}
        title={item.title}
      >
        <div><strong>{item.title}</strong><time dateTime={item.last_activity_at}>{formatActivity(item.last_activity_at)}</time></div>
        <small>{item.last_question || "暂无提问"}</small>
      </NavLink>)}
      {loaded && !items.length && <p className="conversation-history-empty">暂无历史问答</p>}
      {!loaded && <p className="conversation-history-empty">正在加载…</p>}
    </div>
  </section>;
}
