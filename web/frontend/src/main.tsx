import React from "react";
import ReactDOM from "react-dom/client";
import { ConfigProvider } from "tdesign-react";
import zhConfig from "tdesign-react/es/locale/zh_CN";

/* ⚠️ 这一行是必须的，且必须在 ./styles.css **之前**。
 *
 * 坑：tdesign-react 的每个组件只会引入自己那份 CSS（比如 es/select/style/css.js），
 * 而那些 CSS 里全是 `var(--td-bg-color-container)` 这类**引用**；
 * 变量真正的**定义**只在 es/style/index.css（主题文件）里。
 * 漏掉它不会报任何错，但所有 var() 都解析为空，表现为：
 *   · Select 下拉面板背景透明 → 与页面文字重叠，读不清
 *   · Button 的 padding 是 `var(--td-comp-paddingLR-*)` → 声明失效被丢弃 → 文字贴住边框
 * 也就是说这两个看似无关的问题其实是同一个根因（实测：产物 CSS 里
 * --td-bg-color-container 被引用 20 次、定义 0 次）。
 *
 * 顺序也有讲究：先 TDesign 主题，再 styles.css —— 后者要覆盖前者的 token。
 */
import "tdesign-react/es/style/index.css";
import App from "./App";
import "./styles.css";

// 主题定制（学术感）走两条路：
// ConfigProvider 管品牌色，styles.css 覆盖圆角/字体等 TDesign token。
ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <ConfigProvider
      globalConfig={{ classPrefix: "t" }}
      locale={zhConfig}
      theme={{ "--td-brand-color": "#1b3a5c" }}
    >
      <App />
    </ConfigProvider>
  </React.StrictMode>
);
