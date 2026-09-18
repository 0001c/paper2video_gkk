# paper2video 项目长期笔记

## 项目定位

本仓库是「论文研究 / 复现」工作区，不是上游代码的 fork。

- `Paper2Video-main/`：从 GitHub 拉取的**上游开源代码包**（showlab/Paper2Video），
  已在 `.gitignore` 中忽略，**不要修改、不要提交**。属于「素材」而非「本仓库产物」。
- `docs/`：归档的论文原文（PDF）与提取文本，作为对照依据。
- `scripts/`：本仓库自己的辅助脚本。
- `.venv/`：本仓库 Python 虚拟环境（根目录，已 gitignore）。

## 约定

- Python 一律走 `.venv`，不用全局解释器。
- 上游代码只读；分析结论写进 `docs/` 或对话，不回写上游目录。

## 关键事实（2026-09-16 核实）

- 论文 `docs/Paper2Video.pdf` = arXiv 2510.05096v2，标题 *Paper2Video: Automatic Video Generation
  from Scientific Papers*，作者 Zeyu Zhu / Kevin Qinghong Lin / Mike Zheng Shou（Show Lab, NUS）。
- 上游仓库即该论文官方实现，含 PaperTalker 多智能体生成管线 + 四项评测指标代码。
- 数据与模型权重不在仓库内：benchmark 数据集在 HuggingFace `ZaynZhu/Paper2Video`；
  数字人权重需另装 Hallo2（独立环境）。

## 模型选型决策（2026-09-17 定）

用户目标：本地模型全部国产化（优先可用 > 效果），API 侧 LLM 全部换 DeepSeek-V4.1-Flash。
完整方案见 `docs/模型替换方案.md`，核验数据可用 `scripts/hf_check.py` 复跑。

**已定选型**（许可证/参数量均在 HF API 上实测过；详表见 `docs/模型替换方案.md`）：

- 语音合成 → `Qwen/Qwen3-TTS-12Hz-1.7B-Base`（Apache-2.0，替 F5-TTS 的 CC-BY-NC）
- 光标定位 → `mPLUG/GUI-Owl-1.5-4B-Instruct`（MIT；UI-TARS-7B 显存装不下）
- 转写对齐 → `Qwen/Qwen3-ASR-0.6B` + `Qwen/Qwen3-ForcedAligner-0.6B`
- 数字人 → `MeiGen-AI/InfiniteTalk`（Apache-2.0；Hallo2 锁 cu118，在 sm_120 上是死结）
- API 语言+视觉 → **`deepseek-flash`**（原生图像输入、1M 上下文）
- API 视频理解 → `Qwen3.5-Omni-Plus`（DeepSeek 不支持视频输入）

**改造时先记住两条**（能省大量返工）：

1. `wei_utils.get_agent_config()` 有兜底分支 → 换成 `OPENAI_COMPATIBLE_MODEL` 只需设
   `OPENAI_API_BASE`/`OPENAI_API_KEY` + 传 `--model_name_t deepseek-flash`，**不用改代码**。
2. `cursor_gen.py:55` 本就用 `parse_action_to_structure_output(factor=1000, model_type="qwen25vl")`，
   与 GUI-Owl 的 0–1000 归一化坐标同源 → 换模型近乎零改造。

**硬约束**：单卡必须按阶段加载/卸载（多进程会让每个 worker 各持一份权重）；
项目路径含中文 → 权重放英文目录；`HF_HOME` 必须在 E/F 盘。

**许可证雷区**：`tencent/HunyuanVideo-Avatar` 是腾讯社区协议（非商用）→
商业交付只用 Apache-2.0 的 InfiniteTalk。

## H5 可视化改造（2026-09-18）

方案文档 `docs/Web可视化方案.md`。改造**只加外壳、不碰算法**。

**输入支持 `.tex`/`.zip`/`.docx`/`.doc`/`.pdf`**（使用者给的是 Word 与 PDF，不是 tex）。
链路：输入 → 提取（`scripts/paper_ingest.py`，零成本）→ LLM 结构化 → `main.tex`。
**A/B/C 已定**（B2 内联预览；做任务列表+续跑）；**D 建议 React+Vite+TDesign+FastAPI**
（build 产物由后端服务、不引路由与状态库）。

**⚠️ 迁移目标：学校 Linux 服务器**（本机 Windows 只是开发环境，导师演示通过后搬迁）：
- 中文字幕字体**不能硬编码 Windows 字体**（已修：`_cjk_subtitle_font()`，
  Linux 用 Noto Sans CJK，可 `PAPER2VIDEO_CJK_FONT` 覆盖）—— 英文不受影响、
  中文会变方框，这类问题在开发机上永远不出现
- 权重/缓存路径不要出现盘符；GPU 档位不要写死（服务器可能多卡）
- 多人使用要补：任务按人隔离、排队可见、按人统计 token、只监听内网

三条硬约束：
1. **入口是 LaTeX 源码目录**：`--paper_latex_root` 下必须有 `main.tex`。
2. **只需一组 API key**：`OPENAI_API_KEY` / `OPENAI_API_BASE`（DeepSeek）。
3. **`--if_tree_search` 必须写死 False**（默认值 True 会走 `latex_code_gen` → NameError）。

转换层：不用 Office/WPS 的 COM（会连到使用者的 WPS 并挂死）；`.doc` 走 olefile（**有损**）；
PDF 必须行合并且句末判据同时认中英文标点。**真实 Word 不用标题样式（heading=0）**，
所以 `.docx` 并无结构优势，三种格式都靠 LLM 从正文推断章节；
三者内容等价，**pdf 噪声最少、优先用**。

成本口径：`result/<名字>/token.json`；一篇约 5.6 万 token，**讲稿占 76%**，
故 `--reuse_script` 是省钱主力。

**数字人必须打 MuseTalk 偶数尺寸补丁**（`docs/MuseTalk补丁.md`）：
上游 `-vf format=yuv420p` 不处理奇数尺寸，人脸裁框为奇数（如 215x221）时
libx264 拒绝编码 → temp.mp4 为 0 字节 → **整批 11 页"跑完但无产物"**，
症状极具误导性（上游照样打印 result is save、总耗时正常）。
已改 `<MuseTalk>/scripts/{realtime_inference,inference}.py`，加
`crop=trunc(iw/2)*2:trunc(ih/2)*2`；源码里要用**转义双引号**（f-string 定界符）。
⚠️ **改的是上游仓库、不在本仓库 Git 里，学校服务器重装后必须重打**；
自检：`python scripts/verify_musetalk_patch.py`（talking_gen 也会在批量入口提示）。

**stdout 兜底**：上游有 4 处 print 带 emoji（`cursor_render.py` 的 \u2705、
`cursor_gen.py` 与 `slide_code_gen` 的 \u26a0），GBK stdout 下会抛
UnicodeEncodeError，且崩点在流水线**最末端**。已在 `pv_env.py` 模块级做
`stdout/stderr.reconfigure(errors="replace")`（被 9 个模块导入 ⇒ 覆盖所有入口），
配合 web 侧 `PYTHONIOENCODING=utf-8`。

**日志编码必须统一**：`run.log` 同时是父进程写入目标与子进程 stdout 目标，
子进程默认按 Windows 控制台编码（GBK）输出 → 混合编码文件任何单一解码器都读不了，
表现为界面乱码 + **日志解析正则命中 0 条**（不报错）。
已修：子进程加 `PYTHONIOENCODING=utf-8`；读取端 `_read_log_text()` 逐行回落。
**迁 Linux 后这一行仍要保留**（Linux 默认 UTF-8，属无操作，但别删）。

架构：FastAPI + 单 worker 串行队列（单卡并行必 OOM）；流水线走子进程
`Popen(argv, cwd=绝对路径)` 不经 shell；只监听 127.0.0.1。

## H5 P1（2026-09-18 完成）

四件：结构化进度 / 页级可视化 / 逐页素材浏览 / 任务记录+续跑。

- **`src/progress_report.py`**：流水线写 `progress.jsonl`（一行一 JSON）。
  **默认不生效**（未设 `PAPER2VIDEO_PROGRESS_FILE` 就不生成文件）、绝不抛异常、只追加。
  后端 `read_progress` **优先读它、回落日志**，终态以任务状态为准。
- **产物目录读写在 `_result_dir(job)`**（唯一入口）：续跑不复制产物，
  新任务的 `config.result_dir` 指向原任务的目录。**读取侧（产物列表/逐页索引/
  文件服务/总页数）必须走它**，直接拼 `<job_dir>/result` 会让续跑任务看到空产物，
  且"续跑的续跑"会被 400 拒掉。
- **`POST /api/jobs/{id}/rerun`**：只重跑 stage 2/3 + `--reuse_script` ⇒ **零 API 费用**。
  **不复制产物**：新任务指向原 `result` 目录（`_build_argv` 支持 `result_dir` 覆盖）。
- **`GET /api/jobs/{id}/pages`**：服务端拼逐页索引（幻灯片 1-based / 音频 0-based /
  数字人多一层参考图目录，命名不一致，前端别自己拼）。
- **补上 P0 缺口**：`needs_struct` 原本没人用（PDF/Word 输入不会生成 main.tex）
  → 新增 `_prepare_latex()` 前置步骤。

⚠️ 三条"能构建但行为不对"的坑（都靠完整类型检查/测试才发现）：
TDesign 主题 CSS 漏引入（`--td-*` 全未定义）、`ConfigProvider` 只读 `globalConfig`
（locale 被忽略）、build 带 `--noCheck` 掩盖类型错误。现 build 改为 `tsc -b && vite build`。

验证：`scripts/smoke_web_api.py` 共 44 项断言，**零 API 费用**即可复现
SSE/取消/产物 Range/进度优先级/逐页索引/续跑参数。

## 前端体验：四条机制性缺陷（2026-09-18 **已修**，方案见 `docs/前端体验改进方案.md`）

- **阶段与总进度只有一处定义**：`web/app.py` 的 `PHASES`（9 阶段，带权重/有无页数/
  卡住阈值），`/api/options` 下发给前端渲染步骤条；总进度由后端 `_phase_overall_pct` 算，
  **单调不减**；没有逐页进度的阶段只走到该阶段起点（不编造进度）。
- **数字人阶段的进度靠"旁路取数"**：批量路径是"一个进程跑完 N 页、之后才逐页 report"，
  所以真正在跑的 5~10 分钟里 progress.jsonl 零记录（实测 14 条记录时间戳全是同一秒）。
  现在由 `talking_gen._monitor_batch_progress` 后台线程轮询 `musetalk_batch_output()`
  算出的预期产物路径 —— **不改 MuseTalk**。旧文件靠 `mtime + 1.0 >= started` 排除。
- **SSE 帧逻辑抽成了 `_sse_frames()`**（含心跳注释帧 + 断流重连配合）：
  一是因为 **TestClient 对流式响应是缓冲的**，测心跳必须直驱生成器；
  二是帧格式以后只有一处定义。心跳间隔可用 `PAPER2VIDEO_SSE_KEEPALIVE` 覆盖。
- **凭据按任务注入**：`_JOB_KEYS[job_id]`（原先是进程级 `_SESSION_KEYS` ⇒ 多人共用一份，
  "最后输入的人覆盖所有人"）。回落 .env 时必须写 run.log，让回落路径可见。
- **产物/逐页刷新的三个触发点**（换对象 / 状态或阶段变化 / 运行中每 5 秒轮询）：
  只依赖 `job.status` 是单点设计，SSE 一断就永久冻结 —— 凡"只在 X 变化时刷新"都要加轮询兜底。
- 跑测试要 `CODEBUDDY_SAFE_DELETE_ENABLED=0`（沙箱批量删除守卫会拦测试自己的 rmtree）。

- **数字人阶段的进度是「批量跑完后一次性补报」**：`talking_gen.py:536-568` 先让 MuseTalk 一个进程
  跑完 N 页，**之后**才逐页取回产物并 `report()`。实测 14 条记录时间戳全为 `22:58:21`（相差 1~2 ms）。
  后果三连：进度条停在 100%、阶段名停在上一阶段、`stalled_seconds` 涨到 600+ 触发**「疑似卡住」误报**。
  要真实进度只能**旁路取数**（轮询 `musetalk_batch_output()` 算出的预期产物路径，不改 MuseTalk）。
- **进度条口径错误**：`done = talking_done || tts_done` ÷ 总页数，一个条要表达 6 个阶段；
  实测序列含 **100% → 7% 的回跳**，且 stage 1 的 `total=0` 所以条是空的。应改**阶段加权**。
- **SSE 断流即冻结**：`App.tsx` 的 `es.onerror = () => es.close()` **禁用了 EventSource 自带重连**，
  且不提示；而产物/逐页素材**只在 `job.status` 变化时拉一次** → 断流后唯一触发点永不到来，
  产物区要手动刷新才出现。**"单一触发点 + 静默失败"是同一类坏味道**，凡"只在 X 变化时刷新"都要加轮询兜底。
- **`_SESSION_KEYS` 是进程级全局**（`/api/key/verify` 与建任务都写它）→ **多人共用一份 key**：
  迁学校服务器前必须改成按 `job_id` 注入。

## 结构化层（`scripts/paper_struct.py`，2026-09-18 跑通）

链路：输入 → `paper_ingest` 提取（零成本）→ **`paper_struct` LLM 结构化**（花钱）→ `main.tex`。
**分层是刻意的**：出问题能立刻定位是"没提对"还是"没排对"，不用一起返工。

实测（《议程设置》PDF，8283 字符）：20 秒 / **10482 token** → **tectonic 编译成功**，
7 个 `\section` + 10 个 `\subsection`，`finish_reason=stop`。

**输出上限 65536**（`PAPER2VIDEO_MAX_TOKENS` 可覆盖）。长论文会撞上限：实测一篇 24 页/
6.6 万字符的论文把 32768 用满（思维链占大头），main.tex 断在半张表上、
**没有 \end{document}**，而它正是下游生成幻灯片的输入 —— 尾部内容静默缺失。
`paper_struct` 现已：撞上限**续写**（最多 3 轮）→ 仍不全则**截到最后一个
环境配平点 + 补 \end{document}**（实测只丢 1.3%、编译通过）。

prompt 必须写死的四条（均为实测得来）：
1. 中文必须 `ctex`；表格必须 `tabularray` 的 `tblr`（`tabular` 与 ctex 宏包级冲突）
2. **真实论文标题无样式标记**（heading=0），靠内容语义与数字序号识别章节
3. 不得编造数据/文献/图表
4. **不得"顺手纠正"原文** —— 加这条后保真度全面提升（见跨项目记忆）

配套 `scripts/check_struct_fidelity.py` 做双向保真度检查（漏 + 编）。
**主指标必须用固定长度窗口**，不能用整句比对：两边切句方式不同、摘要前缀有无不同，
整句比对会产生大量假阴性，而**假警报比不报更糟**（会让人不再信任报告）。
口径：保留率 93.7% / 可溯源率 93.4%。
