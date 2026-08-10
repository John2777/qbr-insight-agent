import { FormEvent, useState } from "react";
import { api } from "../api";

export function LoginPage({ onAuthenticated }: { onAuthenticated: () => void }) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState("");

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!username || !password || submitting) return;
    setSubmitting(true); setError("");
    try {
      await api("/api/v1/auth/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ username, password })
      });
      setPassword(""); onAuthenticated();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Sign-in failed");
    } finally { setSubmitting(false); }
  }

  return <main className="login-page">
    <section className="login-panel" aria-labelledby="login-title">
      <div className="login-brand"><span className="brand-mark">Q</span><div><strong>QBR Insight</strong><small>Evidence-first agent</small></div></div>
      <div className="login-copy"><span className="eyebrow">SECURE DEMO ACCESS</span><h1 id="login-title">Sign in to the demo workspace</h1><p>Enter the demo credentials. Session credentials are stored only in a secure cookie.</p></div>
      <form onSubmit={submit} className="login-form">
        <label>Username<input autoComplete="username" autoFocus value={username} onChange={(event) => setUsername(event.target.value)} /></label>
        <label>Password<input type="password" autoComplete="current-password" value={password} onChange={(event) => setPassword(event.target.value)} /></label>
        {error && <div className="alert error" role="alert">{error}</div>}
        <button className="login-button" disabled={submitting || !username || !password}>{submitting ? "Verifying…" : "Secure sign in"}</button>
      </form>
      <footer>Repeated failures temporarily limit sign-in attempts · HTTPS is required for public deployments</footer>
    </section>
    <aside className="login-visual"><div><span>01</span><strong>Source data first</strong><p>Parse source data directly from PPT charts and embedded workbooks.</p></div><div><span>02</span><strong>Traceable evidence</strong><p>Every answer links back to its slide and element position.</p></div><div><span>03</span><strong>Safe refusal</strong><p>When evidence is insufficient, external knowledge is not used to fill gaps.</p></div></aside>
  </main>;
}
