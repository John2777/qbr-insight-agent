import { FormEvent, useEffect, useState } from "react";
import { useNavigate, useParams, useSearchParams } from "react-router-dom";
import { api, streamSse } from "../api";
import { ConfidenceBadge } from "../components/ConfidenceBadge";
import { SlideCanvas } from "../components/SlideCanvas";
import { AssistantAnswer } from "../components/AssistantAnswer";
import { MessageCopyButton } from "../components/MessageCopyButton";
import { notifyConversationHistoryChanged } from "../components/ConversationHistory";
import type { Citation, Conversation, DocumentItem, SlideDetail } from "../types";

export function ChatPage() {
  const { conversationId } = useParams(); const navigate = useNavigate(); const [params] = useSearchParams();
  const [conversation, setConversation] = useState<Conversation | null>(null); const [documents, setDocuments] = useState<DocumentItem[]>([]);
  const [selected, setSelected] = useState<string[]>(params.get("document") ? [params.get("document")!] : []);
  const [question, setQuestion] = useState(""); const [sending, setSending] = useState(false); const [citation, setCitation] = useState<Citation | null>(null); const [slide, setSlide] = useState<SlideDetail | null>(null);
  const [streamingAnswer, setStreamingAnswer] = useState(""); const [stage, setStage] = useState(""); const [error, setError] = useState("");
  useEffect(() => { api<{ items: DocumentItem[] }>("/api/v1/documents").then(({ items }) => setDocuments(items.filter((d) => ["ready", "partial"].includes(d.status)))); }, []);
  useEffect(() => {
    let active = true;
    if (!conversationId) {
      setConversation(null);
      setSelected(params.get("document") ? [params.get("document")!] : []);
      return () => { active = false; };
    }
    setError("");
    void api<Conversation>(`/api/v1/conversations/${conversationId}`)
      .then((item) => {
        if (!active) return;
        setConversation(item);
        setSelected(item.scope.document_ids ?? []);
      })
      .catch((err) => { if (active) setError(err instanceof Error ? err.message : "历史问答加载失败"); });
    return () => { active = false; };
  }, [conversationId]);

  async function submit(event: FormEvent) {
    event.preventDefault(); if (!question.trim() || sending) return; setSending(true); setError(""); setStreamingAnswer("");
    try {
      let id = conversationId;
      if (!id) { const created = await api<Conversation>("/api/v1/conversations", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ document_ids: selected, title: question.slice(0, 60) }) }); id = created.id; navigate(`/chat/${id}`, { replace: true }); }
      const sent = await api<{ run_id: string; events_url: string }>(`/api/v1/conversations/${id}/messages`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ content: question, client_message_id: crypto.randomUUID() }) });
      setQuestion(""); setStage("已进入回答队列");
      await streamSse(sent.events_url, (event) => {
        if (event.event === "status") setStage(String(event.data.message ?? "处理中"));
        if (event.event === "answer_delta") setStreamingAnswer((value) => value + String(event.data.delta ?? ""));
        if (event.event === "warning") setStage(String(event.data.message ?? event.data.code ?? "已降级处理"));
      });
      setConversation(await api<Conversation>(`/api/v1/conversations/${id}`)); setStreamingAnswer(""); setStage(""); notifyConversationHistoryChanged();
    } catch (err) { setError(err instanceof Error ? err.message : "回答失败"); }
    finally { setSending(false); }
  }
  async function showEvidence(item: Citation) { setCitation(item); setSlide(await api<SlideDetail>(`/api/v1/slides/${item.slide_id}`)); }
  return (
    <section className="chat-page">
      <div className="chat-column"><header className="chat-header"><div><span className="eyebrow">EVIDENCE QA</span><h1>{conversation?.title || "向 QBR 提问"}</h1></div><select aria-label="文档范围" value={selected[0] ?? ""} disabled={!!conversationId} onChange={(e) => setSelected(e.target.value ? [e.target.value] : [])}><option value="">全部可用文档</option>{documents.map((d) => <option key={d.id} value={d.id}>{d.title}</option>)}</select></header>
        <div className="messages">{!conversation?.messages.length && !streamingAnswer && <div className="chat-empty"><span>⌁</span><h2>从可核验证据开始</h2><p>试试“VONB 是什么意思？”或“Margin 从 Q1 到 Q3 变化多少？”</p></div>}{conversation?.messages.map((message) => <article key={message.id} className={`message ${message.role}`}><div className="message-role">{message.role === "user" ? "你" : "QBR Agent"}</div><div className="message-body">{message.role === "assistant" && message.content ? <AssistantAnswer content={message.content} citations={message.citations ?? []} showVisuals={message.metadata?.show_visuals ?? true} onCitation={(item) => void showEvidence(item)}/> : message.content || (message.status === "running" ? "正在准备回答…" : "")}{message.citations?.length > 0 && <div className="citation-row">{message.citations.map((item) => <button onClick={() => void showEvidence(item)} key={item.id}>{item.label} 第 {item.slide_no} 页</button>)}</div>}</div>{message.content?.trim() && <div className="message-actions"><MessageCopyButton content={message.content} kind={message.role === "user" ? "提问" : "回答"}/></div>}</article>)}{(streamingAnswer || stage) && <article className="message assistant streaming"><div className="message-role">QBR Agent · LIVE</div><div className="message-body">{streamingAnswer || stage}<span className="stream-cursor">▍</span></div></article>}</div>
        {error && <div className="alert error" role="alert">{error}</div>}
        <form className="composer" onSubmit={submit}><textarea aria-label="问题" value={question} onChange={(e) => setQuestion(e.target.value)} placeholder={documents.length ? "询问指标、趋势、差距或原因…" : "请先上传并解析一份文档"} disabled={!documents.length || sending} onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); e.currentTarget.form?.requestSubmit(); } }}/><button disabled={!question.trim() || sending}>{sending ? "检索中…" : "发送"}</button><small>回答只使用当前文档证据；关键数值由结构化工具计算。</small></form>
      </div>
      <aside className="evidence-pane">{citation && slide ? <><header><div><span className="eyebrow">SOURCE</span><h2>{citation.document_title}</h2><p>第 {citation.slide_no} 页 · {citation.element_type}</p></div><button aria-label="关闭证据" onClick={() => setCitation(null)}>×</button></header><SlideCanvas slide={slide} citation={citation}/><blockquote>{citation.quote}</blockquote><ConfidenceBadge confidence={citation.confidence} source={citation.source_kind}/></> : <div className="evidence-empty"><span>▱</span><h2>证据查看器</h2><p>点击回答中的引用，即可在原幻灯片中定位并高亮来源。</p></div>}</aside>
    </section>
  );
}
