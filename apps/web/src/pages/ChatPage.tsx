import { FormEvent, useEffect, useRef, useState } from "react";
import { useNavigate, useParams, useSearchParams } from "react-router-dom";
import { api, streamSse } from "../api";
import { ConfidenceBadge } from "../components/ConfidenceBadge";
import { SlideCanvas } from "../components/SlideCanvas";
import { AssistantAnswer } from "../components/AssistantAnswer";
import { MessageCopyButton } from "../components/MessageCopyButton";
import { notifyConversationsChanged } from "../conversationEvents";
import type { Citation, Conversation, DocumentItem, SlideDetail } from "../types";

export function ChatPage() {
  const { conversationId } = useParams(); const navigate = useNavigate(); const [params] = useSearchParams();
  const [conversation, setConversation] = useState<Conversation | null>(null); const [documents, setDocuments] = useState<DocumentItem[]>([]);
  const [selected, setSelected] = useState<string[]>(params.get("document") ? [params.get("document")!] : []);
  const [question, setQuestion] = useState(""); const [sending, setSending] = useState(false); const [citation, setCitation] = useState<Citation | null>(null); const [slide, setSlide] = useState<SlideDetail | null>(null);
  const [streamingAnswer, setStreamingAnswer] = useState(""); const [stage, setStage] = useState(""); const [error, setError] = useState("");
  const [pendingQuestion, setPendingQuestion] = useState("");
  const messagesRef = useRef<HTMLDivElement>(null);
  const stickToBottomRef = useRef(true);
  const pendingBaselineCountRef = useRef(0);
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
      .catch((err) => { if (active) setError(err instanceof Error ? err.message : "Failed to load conversation"); });
    return () => { active = false; };
  }, [conversationId]);

  useEffect(() => {
    if (!stickToBottomRef.current) return;
    const frame = window.requestAnimationFrame(() => {
      const messages = messagesRef.current;
      if (!messages) return;
      messages.scrollTo({
        top: messages.scrollHeight,
        behavior: streamingAnswer ? "auto" : "smooth",
      });
    });
    return () => window.cancelAnimationFrame(frame);
  }, [conversation?.messages.length, pendingQuestion, sending, stage, streamingAnswer]);

  function handleMessageScroll() {
    const messages = messagesRef.current;
    if (!messages) return;
    const distanceFromBottom = messages.scrollHeight - messages.scrollTop - messages.clientHeight;
    stickToBottomRef.current = distanceFromBottom < 120;
  }

  async function submit(event: FormEvent) {
    event.preventDefault();
    const submittedQuestion = question.trim();
    if (!submittedQuestion || sending) return;
    stickToBottomRef.current = true;
    pendingBaselineCountRef.current = conversation?.messages.length ?? 0;
    setSending(true); setError(""); setStreamingAnswer(""); setStage("Submitting question…");
    setPendingQuestion(submittedQuestion); setQuestion("");
    try {
      let id = conversationId;
      if (!id) { const created = await api<Conversation>("/api/v1/conversations", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ document_ids: selected, title: submittedQuestion.slice(0, 60) }) }); id = created.id; navigate(`/chat/${id}`, { replace: true }); }
      const sent = await api<{ run_id: string; events_url: string }>(`/api/v1/conversations/${id}/messages`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ content: submittedQuestion, client_message_id: crypto.randomUUID() }) });
      setStage("Queued for an answer");
      await streamSse(sent.events_url, (event) => {
        if (event.event === "status") setStage(String(event.data.message ?? "Processing"));
        if (event.event === "answer_delta") setStreamingAnswer((value) => value + String(event.data.delta ?? ""));
        if (event.event === "warning") setStage(String(event.data.message ?? event.data.code ?? "Fallback mode active"));
      });
      setConversation(await api<Conversation>(`/api/v1/conversations/${id}`)); setPendingQuestion(""); setStreamingAnswer(""); setStage(""); notifyConversationsChanged();
    } catch (err) { setPendingQuestion(""); setStage(""); setQuestion(submittedQuestion); setError(err instanceof Error ? err.message : "Failed to answer"); }
    finally { setSending(false); }
  }
  async function showEvidence(item: Citation) { setCitation(item); setSlide(await api<SlideDetail>(`/api/v1/slides/${item.slide_id}`)); }
  const pendingAlreadyPersisted = Boolean(pendingQuestion && conversation?.messages.slice(pendingBaselineCountRef.current).some(
    (message) => message.role === "user" && message.content.trim() === pendingQuestion,
  ));
  const visibleMessages = conversation?.messages.filter(
    (message) => !(message.role === "assistant" && message.status === "running" && (sending || stage || streamingAnswer)),
  ) ?? [];
  const showPendingQuestion = Boolean(pendingQuestion && !pendingAlreadyPersisted);
  const showEmpty = visibleMessages.length === 0 && !showPendingQuestion && !sending && !stage && !streamingAnswer;
  return (
    <section className="chat-page">
      <div className="chat-column"><header className="chat-header"><div><span className="eyebrow">EVIDENCE QA</span><h1>{conversation?.title || "Ask QBR"}</h1></div><select aria-label="Document scope" value={selected[0] ?? ""} disabled={!!conversationId} onChange={(e) => setSelected(e.target.value ? [e.target.value] : [])}><option value="">All available documents</option>{documents.map((d) => <option key={d.id} value={d.id}>{d.title}</option>)}</select></header>
        <div className="messages" ref={messagesRef} onScroll={handleMessageScroll}>{showEmpty && <div className="chat-empty"><span>⌁</span><h2>Start with verifiable evidence</h2><p>Try “What does VONB mean?” or “How did margin change from Q1 to Q3?”</p></div>}{visibleMessages.map((message) => <article key={message.id} className={`message ${message.role}`}><div className="message-role">{message.role === "user" ? "You" : "QBR Agent"}</div><div className="message-body">{message.role === "assistant" && message.content ? <AssistantAnswer content={message.content} citations={message.citations ?? []} showVisuals={message.metadata?.show_visuals ?? true} onCitation={(item) => void showEvidence(item)}/> : message.content || (message.status === "running" ? "Preparing answer…" : "")}{message.citations?.length > 0 && <div className="citation-row">{message.citations.map((item) => <button onClick={() => void showEvidence(item)} key={item.id}>{item.label} · Slide {item.slide_no}</button>)}</div>}</div>{message.content?.trim() && <div className="message-actions"><MessageCopyButton content={message.content} kind={message.role === "user" ? "question" : "answer"}/></div>}</article>)}{showPendingQuestion && <article className="message user pending"><div className="message-role">You</div><div className="message-body">{pendingQuestion}</div></article>}{(streamingAnswer || stage || sending) && <article className="message assistant streaming" aria-live="polite"><div className="message-role">QBR Agent · LIVE</div><div className="message-body">{streamingAnswer || stage || "Preparing answer…"}<span className="stream-cursor">▍</span></div></article>}</div>
        {error && <div className="alert error" role="alert">{error}</div>}
        <form className="composer" onSubmit={submit}><textarea aria-label="Question" value={question} onChange={(e) => setQuestion(e.target.value)} placeholder={documents.length ? "Ask about a metric, trend, gap, or driver…" : "Upload and parse a document first"} disabled={!documents.length || sending} onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); e.currentTarget.form?.requestSubmit(); } }}/><button disabled={!question.trim() || sending}>{sending ? "Searching…" : "Send"}</button><small>Answers use only evidence from the selected documents; structured tools calculate key figures.</small></form>
      </div>
      <aside className="evidence-pane">{citation && slide ? <><header><div><span className="eyebrow">SOURCE</span><h2>{citation.document_title}</h2><p>Slide {citation.slide_no} · {citation.element_type}</p></div><button aria-label="Close evidence" onClick={() => setCitation(null)}>×</button></header><SlideCanvas slide={slide} citation={citation}/><blockquote>{citation.quote}</blockquote><ConfidenceBadge confidence={citation.confidence} source={citation.source_kind}/></> : <div className="evidence-empty"><span>▱</span><h2>Evidence Viewer</h2><p>Select a citation in an answer to locate and highlight its source on the original slide.</p></div>}</aside>
    </section>
  );
}
