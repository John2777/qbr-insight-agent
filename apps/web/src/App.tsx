import { useEffect, useState } from "react";
import { NavLink, Navigate, Route, Routes } from "react-router-dom";
import { api } from "./api";
import { ChatPage } from "./pages/ChatPage";
import { DocumentDetailPage } from "./pages/DocumentDetailPage";
import { DocumentsPage } from "./pages/DocumentsPage";
import { ReviewsPage } from "./pages/ReviewsPage";
import { AnalyticsPage } from "./pages/AnalyticsPage";
import { LoginPage } from "./pages/LoginPage";
import { ConversationHistory } from "./components/ConversationHistory";

export function App() {
  const [authState, setAuthState] = useState<"checking" | "authenticated" | "anonymous">("checking");
  useEffect(() => {
    void api("/api/v1/auth/session").then(() => setAuthState("authenticated")).catch(() => setAuthState("anonymous"));
    const requireLogin = () => setAuthState("anonymous");
    window.addEventListener("qbr-auth-required", requireLogin);
    return () => window.removeEventListener("qbr-auth-required", requireLogin);
  }, []);
  async function logout() {
    try { await api("/api/v1/auth/logout", { method: "POST" }); }
    finally { window.localStorage.removeItem("qbr_api_token"); setAuthState("anonymous"); }
  }
  if (authState === "checking") return <div className="auth-loading"><span className="spinner"/><p>Verifying your session…</p></div>;
  if (authState === "anonymous") return <LoginPage onAuthenticated={() => setAuthState("authenticated")} />;
  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="brand">
          <span className="brand-mark">Q</span>
          <div><strong>QBR Insight</strong><small>Evidence-first agent</small></div>
        </div>
        <nav aria-label="Main navigation">
          <NavLink to="/documents">Document Library</NavLink>
          <NavLink to="/chat">Ask QBR</NavLink>
          <NavLink to="/reviews">Reviews</NavLink>
          <NavLink to="/analytics">Analytics</NavLink>
        </nav>
        <ConversationHistory />
        <div className="sidebar-note"><span className="status-dot" /> Verified session<br/><small>Evidence stays within this workspace</small><button onClick={() => void logout()}>Sign out</button></div>
      </aside>
      <main className="main-content">
        <Routes>
          <Route path="/" element={<Navigate to="/documents" replace />} />
          <Route path="/documents" element={<DocumentsPage />} />
          <Route path="/documents/:documentId" element={<DocumentDetailPage />} />
          <Route path="/chat" element={<ChatPage />} />
          <Route path="/chat/:conversationId" element={<ChatPage />} />
          <Route path="/reviews" element={<ReviewsPage />} />
          <Route path="/analytics" element={<AnalyticsPage />} />
        </Routes>
      </main>
    </div>
  );
}
