import React from "react";
import ReactDOM from "react-dom/client";
import { ConfigProvider } from "tdesign-react";
import zhConfig from "tdesign-react/es/locale/zh_CN";
import App from "./App";
import "./styles.css";

// 主题定制（学术感）走 ConfigProvider + CSS 变量两条路：
// ConfigProvider 管品牌色，styles.css 里覆盖圆角/字体等 TDesign token。
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
