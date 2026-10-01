export type Theme = "light" | "dark";
const STORAGE_KEY = "paper2video.theme";
let switching = false;

export function currentTheme(): Theme {
  return document.documentElement.getAttribute("theme-mode") === "dark" ? "dark" : "light";
}

function applyTheme(theme: Theme) {
  document.documentElement.setAttribute("theme-mode", theme);
  document.documentElement.style.colorScheme = theme;
  try { localStorage.setItem(STORAGE_KEY, theme); } catch { /* 存储被禁用时仍可切换。 */ }
}

type Click = { detail: number; clientX: number; clientY: number;
  currentTarget?: { getBoundingClientRect(): Pick<DOMRect, "left" | "top" | "width" | "height"> } };

export async function toggleTheme(event: Click, onChange: (theme: Theme) => void) {
  if (switching) return;
  const next = currentTheme() === "dark" ? "light" : "dark";
  const commit = () => { applyTheme(next); onChange(next); };
  if (typeof document.startViewTransition !== "function" || matchMedia("(prefers-reduced-motion: reduce)").matches) {
    commit();
    return;
  }
  const rect = event.currentTarget?.getBoundingClientRect();
  const x = event.detail > 0 ? event.clientX : rect ? rect.left + rect.width / 2 : innerWidth - 48;
  const y = event.detail > 0 ? event.clientY : rect ? rect.top + rect.height / 2 : 48;
  const radius = Math.hypot(Math.max(x, innerWidth - x), Math.max(y, innerHeight - y));
  const clipPath = [`circle(0px at ${x}px ${y}px)`, `circle(${radius}px at ${x}px ${y}px)`];
  // 新快照出现到 ready 回调之间也必须保持裁切，避免亮色先全屏闪出。
  document.documentElement.style.setProperty('--theme-x', `${x}px`);
  document.documentElement.style.setProperty('--theme-y', `${y}px`);
  switching = true;
  let transition: ViewTransition | undefined;
  try {
    // 改主题和 React 按钮状态均在快照回调内同步完成。
    transition = document.startViewTransition(commit);
    await transition.ready;
    document.documentElement.animate(
      { clipPath: next === "dark" ? [...clipPath].reverse() : clipPath },
      { duration: 600, easing: "ease-in-out", fill: "both", pseudoElement: next === "dark" ? "::view-transition-old(root)" : "::view-transition-new(root)" },
    );
    await transition.finished;
  } catch {
    transition?.skipTransition();
    commit();
    await transition?.finished.catch(() => {});
  } finally { switching = false; }
}
