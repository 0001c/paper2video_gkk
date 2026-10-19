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

### 流程演示 `/demo`

构建后访问 `http://127.0.0.1:8848/demo`（静态服务会规范化为 `/demo/`）。这是独立的前端入口，沿用现有访问权限，不需要重启后端。

演示固定使用 `job_1019_192023_1092` 的真实产物，包含素材、教案、20 页课件与讲稿、逐页配音、成片与评价。讲稿试改、审核确认和问答只保存在页面内存，刷新或“重新体验”即重置；不发送任务写请求，不调用模型。音频与成片保持原任务内容。

白名单快照位于 `frontend/src/demo/sample.json`，只包含展示所需的内容，不导出模型连接、密钥或本机路径。原任务存在时，可在项目根目录运行 `.venv/Scripts/python.exe scripts/export_demo.py` 重新导出，然后重新构建前端。媒体通过原有受保护的 `/api/jobs/{id}/files/` 接口读取，因此必须保留原任务产物；页面不可作为脱离后端的独立离线站点。

原文与教案保留历史任务快照，其中可能包含旧 PDF 提取器的页眉/章节归属误差。评价展示真实通过线、最低页得分和待复核证据。

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

### ⚠️ 这个预检为什么必须按平台分开写

`SO_REUSEADDR` 在两个平台上的语义**正好相反**，所以这段代码很容易
"修好一个平台、弄坏另一个"，而单平台跑一遍看不出来（第一版就踩了）：

| | Windows | POSIX |
|---|---|---|
| `SO_REUSEADDR` 含义 | 允许 bind 到一个**已被别的进程占用**的端口（抢占） | 仅允许复用 **TIME_WAIT** 的地址 |
| 用它探测的结果 | **永远报"空闲"** → 预检形同虚设 | 正确 |
| 不用它探测的结果 | 正确 | 刚关服务就重启会把 **TIME_WAIT 误判成"被占用"** → 拦下本来能启动的端口 |
| 结论 | 探测时**不设**，加 `SO_EXCLUSIVEADDRUSE` | 探测时**要设**（与服务端 asyncio 的 `reuse_address=True` 一致） |

判据刻意只回答"**本服务能不能起来**"，不去猜别的进程状态 —— 猜端口占用容易产生
假阳性（别的进程占 `0.0.0.0`、我们绑 `127.0.0.1`；或只剩 TIME_WAIT），
而**假阳性会把一个能用的端口拦下来**，比假阴性更讨厌。

用 `python scripts/smoke_web_port.py` 可复现验证：它把"平台差异"从运行期行为
变成可断言的静态事实（选项层），再叠加行为层与真实启动路径。**迁到 Linux 后请重跑一次。**

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

## 接口一览（P1 后）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/options` | 语言/类型档位（从 `language_config` 反射，前端不硬编码）|
| POST | `/api/ingest` | 上传论文 → 提取正文 → 预览 |
| POST | `/api/jobs` | 创建任务（需要 `ingest_id` + `portrait_path`；PDF/Word 还要 `source_path`）|
| GET | `/api/jobs` | 任务记录列表 |
| GET | `/api/jobs/{id}` | 单个任务（含实时进度）|
| GET | `/api/jobs/{id}/events` | SSE 进度流（只在内容变化时推，终态即结束）|
| POST | `/api/jobs/{id}/cancel` | 取消（杀子进程）|
| POST | `/api/jobs/{id}/rerun` | **续跑：默认只重跑 stage 2/3 且复用讲稿 ⇒ 零 API 费用** |
| GET | `/api/jobs/{id}/artifacts` | 产物列表 |
| GET | `/api/jobs/{id}/pages` | 逐页素材索引（幻灯片/音频/数字人/讲稿）|
| GET | `/api/jobs/{id}/files/{rel}` | 产物文件（自带 Range，视频可拖进度条）|
| GET | `/api/system/info` | 硬件 + 耗时标定值 |

### 进度的两个来源，优先结构化

1. `progress.jsonl` —— 流水线主动写（`src/progress_report.py`，一行一个 JSON）。
   由环境变量 `PAPER2VIDEO_PROGRESS_FILE` 开关；**不设置就不生成任何文件**，
   所以命令行直接跑 `pipeline.py` 的行为与以前完全一致。
2. `run.log` + 正则 —— 兜底。零改造但脆（改一句 print 就失效），只在老任务
   目录没有 jsonl 时用到。

终态（已完成/失败/已取消）一律以**任务状态**为准，不从日志文本猜。

### 改前端后请跑一次类型检查

```bash
npm run typecheck     # 等价于 tsc -b --force
```

**为什么单独列出**：`build` 脚本曾经带 `--noCheck`（只转译不检查），
于是类型错误不会让构建失败 —— 已经因此潜伏过两个 bug
（`ConfigProvider` 的 `locale` 传错被静默忽略、TDesign 主题 CSS 漏引入）。
现已改为 `tsc -b && vite build`，但这个教训值得记住：
**「能构建但行为不对」比构建失败难发现得多。**

## ⚠️ 改前端样式前必读：TDesign 的主题 CSS 必须显式引入

`src/main.tsx` 里这一行**不能删**，而且必须在 `./styles.css` 之前：

```ts
import "tdesign-react/es/style/index.css";
```

原因：tdesign-react 的**每个组件**只会引入自己那份 CSS，而那些 CSS 里全是
`var(--td-bg-color-container)` 这类**引用**；变量真正的**定义**只在上面这个主题文件里。
漏掉它**不会报任何错**，但所有 `var()` 解析为空，表现为两个看着毫不相关的问题：

| 症状 | 机理 |
|---|---|
| Select 下拉面板**背景透明**，与页面文字重叠 | `.t-popup__content` 的 `background-color` 是 `var(...)`，解析为空 |
| Button **文字贴住边框** | `padding: var(--td-comp-paddingLR-*)` 整条声明失效被丢弃 → padding 归零 |

实测证据：产物 CSS 里 `--td-bg-color-container` 被**引用 20 次、定义 0 次**。
判断方法：`grep -o 'var(--td-bg-color-container' static/assets/*.css | wc -l`
与 `grep -o -- '--td-bg-color-container:' static/assets/*.css | wc -l` 应大致相当。

`styles.css` 末尾对按钮内边距与下拉面板背景用了**字面量**（而非 `var(--td-*)`）兜底，
即使主题文件再次缺失，也不会退化到"看不清"。

---

## 耗时估算的依据

顶部硬件栏与页脚的预计时长都来自 `/api/system/info`，它同时下发**标定值**
（`CALIBRATION`，见 `app.py`），目的是让"估算依据"只有一处来源。

标定值来自本机两次真实运行的 stage 计时，且能同时复现这两次：

| 本次 | 算得 | 实际 | 误差 |
|---|---|---|---|
| `gkk_en` 15 页 · 英文讲稿 | 872 s (14.5 min) | 868 s | +0.4% |
| `gkk_zh` 11 页 · 中文讲稿 | 929 s (15.5 min) | 934 s | −0.6% |

两点值得记住：**中文每页明显更慢**（TTS 43.6s/页 vs 英文 23.3s/页，
且音频更长会连带拖慢数字人），所以页数少的中文组合可能比页数多的英文组合更久。

`perf_factor` 是**启发式**（按显存档位折算，标定机 16GB = 1.0），
本机只有一块卡、没有别的硬件可标定，界面上必须标明是粗估。
换硬件后若要认真估算，应在目标机器上重跑一次并更新 `CALIBRATION`。

---

## 目录

```
web/
  app.py          FastAPI 后端（含串行队列、SSE 进度、产物服务、硬件探测与耗时标定）
  static/         前端构建产物（vite build 输出，勿手改）
  frontend/       前端源码（React + Vite + TDesign）
    src/main.tsx      ⚠️ 必须引入 TDesign 主题 CSS（见上）
    src/App.tsx       四个区块 + SSE 进度 hook + 硬件栏与耗时估算
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
