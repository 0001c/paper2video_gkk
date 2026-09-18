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

/* 关于 ConfigProvider 的两个坑（都是靠"跑一次完整类型检查"才发现的）：
 *
 * 1. **它只读 `globalConfig` 与 `notSet`**，没有 `locale` / `theme` 这两个 prop。
 *    所以 `<ConfigProvider locale={zhConfig}>` 里的 locale 会被**静默忽略**，
 *    组件内的中文文案（如"请选择""清空"）仍是英文 —— 界面看着正常，只是文案不对。
 *    正确写法是把 locale 对象的字段**展开进 globalConfig**：
 *    每个 locale 文件（这里是 es/locale/zh_CN）的默认导出本身就是一个 GlobalConfigProvider。
 *
 * 2. 品牌色**不能**通过 props 配（没有 theme prop），走 styles.css 的 :root 覆盖
 *    `--td-brand-color` 即可，那里同时管圆角与字体。
 *
 * 注：项目的 build 脚本带 `--noCheck`（不检查类型），这类错误不会让构建失败 ——
 * 改动前端后建议单独跑一次 `tsc -b --force`。
 */
ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <ConfigProvider globalConfig={{ ...zhConfig, classPrefix: "t" }}>
      <App />
    </ConfigProvider>
  </React.StrictMode>
);
