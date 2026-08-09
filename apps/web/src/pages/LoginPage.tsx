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
      setError(err instanceof Error ? err.message : "登录失败");
    } finally { setSubmitting(false); }
  }

  return <main className="login-page">
    <section className="login-panel" aria-labelledby="login-title">
      <div className="login-brand"><span className="brand-mark">Q</span><div><strong>QBR Insight</strong><small>Evidence-first agent</small></div></div>
      <div className="login-copy"><span className="eyebrow">SECURE DEMO ACCESS</span><h1 id="login-title">登录演示空间</h1><p>请输入面试演示账号。会话凭据仅保存在安全 Cookie 中。</p></div>
      <form onSubmit={submit} className="login-form">
        <label>用户名<input autoComplete="username" autoFocus value={username} onChange={(event) => setUsername(event.target.value)} /></label>
        <label>密码<input type="password" autoComplete="current-password" value={password} onChange={(event) => setPassword(event.target.value)} /></label>
        {error && <div className="alert error" role="alert">{error}</div>}
        <button className="login-button" disabled={submitting || !username || !password}>{submitting ? "正在验证…" : "安全登录"}</button>
      </form>
      <footer>连续失败会暂时限制登录 · 公网部署强制 HTTPS</footer>
    </section>
    <aside className="login-visual"><div><span>01</span><strong>源数据优先</strong><p>直接解析 PPT 图表和内嵌工作簿。</p></div><div><span>02</span><strong>证据可追溯</strong><p>每条回答回链到幻灯片和元素位置。</p></div><div><span>03</span><strong>安全拒答</strong><p>证据不足时不使用外部常识补齐。</p></div></aside>
  </main>;
}
