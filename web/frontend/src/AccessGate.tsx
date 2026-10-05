import { createContext, useContext, useEffect, useRef, useState, type FormEvent, type ReactNode } from 'react';
import { Button } from 'tdesign-react';
import { api, errorMessage, runExclusive } from './flow';
import { ThemeToggle } from './ThemeToggle';

type AccessStatus = { enabled: boolean; authenticated: boolean; username?: string | null };
const AccessContext = createContext<{ username: string; logout: () => void; busy: boolean } | null>(null);

export function AccessControls() {
  const access = useContext(AccessContext);
  if (!access) return null;
  return <div className="access-controls"><span>{access.username} · 已登录</span>
    <Button variant="outline" size="small" loading={access.busy} onClick={access.logout}>退出登录</Button></div>;
}

export function AccessGate({ children }: { children: ReactNode }) {
  const [status, setStatus] = useState<AccessStatus | null>(null);
  const [username, setUsername] = useState('admin');
  const [password, setPassword] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const lock = useRef(false);

  const refresh = async () => {
    try {
      const next = await api.get('/api/auth/status') as AccessStatus;
      if (typeof next.enabled !== 'boolean' || typeof next.authenticated !== 'boolean') throw new Error('登录状态响应不合法');
      setStatus(next);
      setError('');
      if (next.enabled && next.authenticated && window.location.pathname === '/login') window.location.replace('/');
    } catch (failure) { setError(errorMessage(failure)); }
  };

  useEffect(() => {
    void refresh();
    const timer = window.setInterval(() => { void refresh(); }, 60_000);
    const expired = () => { setStatus({ enabled: true, authenticated: false }); setPassword(''); setError('登录已过期，请重新登录'); };
    window.addEventListener('paper2video:auth-expired', expired);
    return () => { window.clearInterval(timer); window.removeEventListener('paper2video:auth-expired', expired); };
  }, []);

  const login = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    await runExclusive(lock, setBusy, async () => {
      setError('');
      await api.post('/api/auth/login', { username, password });
      setPassword('');
      window.location.replace('/');
    }, (failure) => { setPassword(''); setError(errorMessage(failure)); });
  };

  const logout = () => { void runExclusive(lock, setBusy, async () => {
    await api.post('/api/auth/logout');
    setStatus({ enabled: true, authenticated: false });
    setPassword('');
    window.location.replace('/login');
  }, (failure) => { setError(errorMessage(failure)); }); };

  if (status && (!status.enabled || status.authenticated)) {
    return <AccessContext.Provider value={status.enabled ? { username: status.username || username, logout, busy } : null}>
      {error && <div className="access-notice" role="alert">{error}</div>}{children}
    </AccessContext.Provider>;
  }

  return <main className="access-page">
    <div className="access-top"><span className="brand">讲解工坊<span className="brand-caption">安全访问</span></span><ThemeToggle /></div>
    <section className="access-card" aria-labelledby="access-title">
      <p className="eyebrow">PRIVATE WORKSPACE / HTTPS</p>
      <h1 id="access-title">登录内容工作台</h1>
      <p className="access-description">课件、声音与视频，只向获准访问的人开放。</p>
      {!status ? <div className="access-status" role="status">{error || '正在确认访问权限…'}
        {error && <Button variant="outline" onClick={() => { void refresh(); }}>重新连接</Button>}</div>
        : <form className="access-form" onSubmit={login}>
          <div><label htmlFor="access-username">管理员账号</label><input id="access-username" name="username" autoComplete="username"
            value={username} onChange={(event) => setUsername(event.target.value)} required maxLength={64} disabled={busy} autoFocus /></div>
          <div><label htmlFor="access-password">登录密码</label><input id="access-password" name="password" type="password" autoComplete="current-password"
            value={password} onChange={(event) => setPassword(event.target.value)} required maxLength={256} disabled={busy} aria-describedby={error ? 'access-error' : undefined} /></div>
          {error && <p id="access-error" className="access-error" role="alert">{error}</p>}
          <Button type="submit" block loading={busy} disabled={!username || !password}>登录工作台</Button>
        </form>}
      <div className="access-footer"><span className="access-lock" aria-hidden="true">◇</span><p>会话最长保留 8 小时。重启服务或退出登录后需重新验证；登录密码不会写入页面本地存储。</p></div>
    </section>
    <p className="access-caption">PAPER2VIDEO · 内容制作工作台</p>
  </main>;
}
