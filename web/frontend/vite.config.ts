import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// 构建产物交给 FastAPI 用 StaticFiles 直接服务 —— 运行时只跑一个进程。
// 开发时才用 `pnpm dev`（HMR），并且把 /api 代理到后端。
//
// ⚠️ 本机有 TUN 代理（HTTP_PROXY=127.0.0.1:13960）。Node 的 fetch 会读环境变量里的
// 代理设置，导致 dev server 连不上本机的 8848。所以这里显式把 127.0.0.1 加入
// no_proxy，否则表现为「后端明明活着，vite 代理却报 502」。
process.env.NO_PROXY = [process.env.NO_PROXY, "127.0.0.1", "localhost"]
  .filter(Boolean)
  .join(",");
process.env.no_proxy = process.env.NO_PROXY;

export default defineConfig({
  plugins: [react()],
  base: "./",                  // 由后端挂在根路径，用相对路径最稳
  build: {
    outDir: "../static",       // 直接产出到 web/static，后端启动即可服务
    emptyOutDir: true,
    chunkSizeWarningLimit: 1200,
    rollupOptions: {
      input: { main: 'index.html', demo: 'demo/index.html', presentation: 'presentation/index.html' },
    },
  },
  server: {
    host: "127.0.0.1",
    port: 5173,
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8848",
        changeOrigin: false,
        // 双保险：即使环境变量漏进 Node，也让代理直连本机
        configure: (proxy) => {
          proxy.on("proxyReq", (proxyReq) => proxyReq.removeHeader("proxy-connection"));
        },
      },
    },
  },
});
