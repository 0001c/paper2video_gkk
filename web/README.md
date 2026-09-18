# Paper2Video Web

把命令行流水线包成一个网页：**上传素材 → 选语言 → 填 API key → 点开始 → 看进度 → 拿成片**。

改造**只加外壳，不碰算法** —— TTS / 光标 / 数字人的代码一律没动。

---

## 快速开始

```bash
# 1) 先构建前端（只需在改动前端后做一次）
cd web/frontend
pnpm install
pnpm build            # 产物直接输出到 web/static

# 2) 启动服务（前端产物由它一并服务，运行时只有一个进程）
cd ../..
.venv/Scripts/python.exe web/app.py
# 打开 http://127.0.0.1:8848
```

开发前端时用 `pnpm dev`（HMR，`/api` 已代理到 8848），改完记得 `pnpm build` 让后端服务到最新产物。

---

## 四条必须守住的设计约束

这四条不是风格偏好，是踩过坑之后定下来的，改代码时不要绕过：

| 约束 | 原因 |
|---|---|
| **只监听 `127.0.0.1`** | 这个界面上有 API key 和文件系统访问。监听 `0.0.0.0` 等于把 `jobs/` 里所有产物暴露给同网络的人 |
| **单 worker 串行队列** | 单卡 16GB，两个任务同时跑必然 OOM。多组合是**排队**而不是并行 |
| **子进程 + 不经 shell** | 显存靠进程退出释放；且本机 MSYS 的 `cd` 遇到含中文的路径会失败 |
| **`--if_tree_search` 写死 `False`，不暴露到界面** | `pipeline.py:31` 那行 import 是注释掉的，而默认值 `True` 会走 `latex_code_gen` → **NameError**。一个必然崩溃的开关没有暴露的意义 |

---

## 端口与环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `P2V_WEB_HOST` | `127.0.0.1` | 改它之前请先想清楚风险 |
| `P2V_WEB_PORT` | `8848` | |
| `PAPER2VIDEO_CJK_FONT` | 平台相关 | 中文字幕字体。Linux 默认 `Noto Sans CJK SC` |

端口也可以用命令行参数指定（**优先级高于环境变量**）：

```bash
python web/app.py --port 8849        # 换一个端口
python web/app.py --auto-port        # 端口被占时自动顺延，不退出
python web/app.py --help             # 看全部选项
```

**8848 被占时会发生什么**：服务会在启动前自行探测并打印占用者，而不是抛
`[Errno 10048] 通常每个套接字地址只允许使用一次`（那条错误既不告诉你是谁占着、
也不告诉你怎么改）。输出形如：

```
端口 8848 已被占用：python.exe (pid=24780, 已运行 0 分钟)。
解决办法（任选其一）：
  1) 换端口：  python web/app.py --port 8849
  2) 自动顺延：python web/app.py --auto-port
  3) 结束占用进程后重试
```

> 最常见的占用者就是**上一次没关掉的自己**。Windows 上用
> `netstat -ano | findstr :8848` 看 pid，再 `taskkill /PID <pid> /F`。
>
> 实现上有个 Windows 专属坑：探测端口时**不能设 `SO_REUSEADDR`** ——
> 该选项在 Windows 的语义与 Linux 相反，允许 bind 到已被占用的端口，
> 会让预检永远返回"空闲"。代码里已写成注释，改这段时别加回去。

---

## ⚠️ 两个会骗过排查的环境坑

**① 本机 TUN 代理会拦截 `127.0.0.1` 的请求。**

本机 `HTTP_PROXY` / `HTTPS_PROXY` 指向 `http://127.0.0.1:13960`。
`urllib` / `requests` / Node 的 fetch **默认会把这个代理也应用到发往本机的请求上**，
结果是 **502 Bad Gateway**，而**服务端日志里完全没有这条请求**（请求根本没到达）——
非常容易误判成"服务挂了"或"接口写错了"。

- 测试脚本：`requests.Session().trust_env = False`
- 或设环境变量：`no_proxy=127.0.0.1,localhost`
- 浏览器通常自动绕过 localhost；若打开页面 502，先查代理

**② 检查后台服务是否活着时，别看"上一条命令里还好好的"。**

用 `&` 起的后台进程会随那条命令一起结束。常驻服务请用工具/终端的正规后台机制，
或直接在这个终端里前台运行。

---

## 目录

```
web/
  app.py          FastAPI 后端（含串行队列、SSE 进度、产物服务）
  static/         前端构建产物（pnpm build 输出，勿手改）
  frontend/       前端源码（React + Vite + TDesign）
    src/App.tsx       四个区块 + SSE 进度 hook
    src/styles.css    学术感主题（覆盖 TDesign 的圆角与配色 token）
jobs/            运行时产生的任务目录（已在 .gitignore）
  <job_id>/
    input/        上传的素材
    ingest/       提取出的正文（paper_text.md）
    result/       流水线产物（含 3_merage.mp4）
    run.log       完整日志（进度就是从这里解析的）
    job.json      任务状态
```

---

## 界面设计说明

采用**学术出版物**的视觉语言，而不是常见的"企业后台"风格。三条具体做法：

1. **圆角压到 2~4px**（TDesign 默认 8px）—— 圆角越大越"互联网"；
2. **标题用衬线族**（Georgia / 宋体），正文留无衬线以保证可读性；
3. **主色用墨蓝 `#1b3a5c`**，背景用极浅暖白 `#fbfaf8`，像纸张而不是屏幕。

进度反馈一律**内联**在发生的位置（细进度条 + 逐页方点 + 日志尾巴），
**不用模态遮罩** —— 一次生成 15~25 分钟，使用者会想切标签页或翻看已生成的幻灯片图。

---

## 已知局限

- **`.docx` 的标题层级不可依赖**：实测使用者手写的 Word 普遍不使用标题样式
  （`python-docx` 读到 heading 数为 0），三种输入格式都得靠 LLM 从正文推断章节。
- **结构化会让 LLM 做排版，它有"顺手纠正"的倾向**（实测会把原文混用的
  `Elisha`/`Elishar` 统一）。prompt 里已明确禁止，但遇到原文刻意的用词差异仍需人工看一眼。
- 前端**没有自动化测试**，验收走人工（见 `docs/Web可视化方案.md` 第 10 节的测试要点清单）。
