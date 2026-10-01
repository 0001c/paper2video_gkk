import { useState } from "react";
import { flushSync } from "react-dom";
import { currentTheme, toggleTheme } from "./theme";

export function ThemeToggle() {
  const [theme, setTheme] = useState(currentTheme);
  const [busy, setBusy] = useState(false);
  const label = theme === "dark" ? "切换到明亮模式" : "切换到暗色模式";
  return <button type="button" className="theme-toggle" aria-label={label} title={label}
    disabled={busy} onClick={async (event) => {
      flushSync(() => setBusy(true));
      try { await toggleTheme(event, (next) => flushSync(() => setTheme(next))); }
      finally { setBusy(false); }
    }}>
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      {theme === "dark" ? <><circle cx="12" cy="12" r="4" /><path d="M12 2v2m0 16v2M2 12h2m16 0h2M5 5l1.4 1.4m11.2 11.2L19 19M5 19l1.4-1.4M17.6 6.4L19 5" /></>
        : <path d="M20.5 13A8.5 8.5 0 0 1 11 3.5 8.5 8.5 0 1 0 20.5 13Z" />}
    </svg>
    <span>{theme === "dark" ? "明亮" : "暗色"}</span>
  </button>;
}
