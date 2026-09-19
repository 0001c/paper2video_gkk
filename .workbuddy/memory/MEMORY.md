# paper2video 项目长期笔记

> 只留**规则与判据**；推导在 `docs/`。
> 改造全貌 `docs/代码改造总览.md` · 上游补丁总账 `docs/上游补丁清单.md`

## 项目定位与约定

- `Paper2Video-main/`：上游开源包。**它在 Git 里**（`src/` 下 172 个文件被跟踪）。
  早先笔记写的"已忽略、不要修改、不要提交"是**错的** —— 我们**确实会改它**，改动须同时落到
  `docs/` 与提交信息里。**唯一不在本仓库 Git 里的是 MuseTalk**（独立仓库）——"重装必重打"只对它成立。
- `docs/` 归档结论；`scripts/` 自研脚本；`web/` 服务层；`jobs/`、`result/` 产物。Python 走 `.venv`。
- 论文 = arXiv 2510.05096v2（Show Lab, NUS）；benchmark 在 HF `ZaynZhu/Paper2Video`。

## 模型选型（`docs/模型替换方案.md`）

本地国产化（可用 > 效果），API 侧全用 DeepSeek-V4.1-Flash。

- TTS `Qwen3-TTS-12Hz-1.7B-Base`(Apache) · 光标 `GUI-Owl-1.5-4B-Instruct`(MIT) ·
  转写对齐 `Qwen3-ASR-0.6B` + `Qwen3-ForcedAligner-0.6B` · 音色相似 `CAM++` ·
  API 语言+视觉 `deepseek-flash` · API 视频 `Qwen3.5-Omni-Plus`
- **数字人 = `MuseTalk`（默认档 `musetalk-local`）**。换型三次：Hallo2（锁 cu118）→ InfiniteTalk →
  LongCat（整帧 480p、注意力序列 36480、依赖 flash-attn）→ **MuseTalk**（256×256 人脸区、序列
  1024、权重 3.8GB、标准 UNet2DConditionModel）。`talking_config.py` 里 musetalk 与 longcat
  **两个 backend 都保留**。旧笔记写 InfiniteTalk、项目讲解写 LongCat，均已陈旧（2026-09-19 修正）。
- **省返工**：`wei_utils.get_agent_config()` 有兜底 ⇒ 换模型只设 `OPENAI_API_BASE/KEY` +
  `--model_name_t`，**不改代码**；`cursor_gen.py:55` 本就 `factor=1000`，与 GUI-Owl 同源。
- **硬约束**：单卡按阶段加载/卸载（上游 `multiprocessing.Pool` 单卡必 OOM）；路径含中文 ⇒
  权重放英文目录；`HF_HOME` 在 E/F 盘。**许可雷区**：`HunyuanVideo-Avatar` 非商用。
- **DeepSeek 是推理模型**：`max_tokens` 给小了返回空 `content`（被 reasoning 吃掉）**且不报错**，
  必须看 `finish_reason`。

## 迁移目标：学校 Linux 服务器（本机 Windows 只是开发机）

- 中文字幕字体**不能硬编码 Windows 字体**（`_cjk_subtitle_font()`，`PAPER2VIDEO_CJK_FONT` 可覆盖）
  —— 英文正常、中文变方框。权重/缓存路径不要出现盘符；GPU 档位不要写死。
- **平台编码**：Windows 上 `open()` 不带 encoding 用 **cp936**，Linux 上是 UTF-8。
  我们链路 **11 处**已显式 `encoding="utf-8"`；子进程环境加 **`PYTHONUTF8=1`**
  （`web/app.py::_run_job`、`scripts/gkk_3combos.py`）——**推翻了早先"只设 PYTHONIOENCODING"
  的决定**（上游 92 处 `open()` 假设 UTF-8）。自检 `verify_tex_encoding.py` + 冒烟第 13 组。
  ⚠️ **判据：编译/命令退出码 0 ≠ 内容正确** —— GBK 写出的 `.tex` 让 tectonic 返回 0，
  却把非 ASCII 全替换成 U+FFFD。
- **MuseTalk 补丁不在本仓库 Git 里 → 重装后必须重打**（`scripts/patch_musetalk.py`）。
- 多人使用还缺：任务按人隔离（**凭据已按 job 隔离**）、排队可见、按人统计 token、只监听内网。
- 跑冒烟要 `CODEBUDDY_SAFE_DELETE_ENABLED=0`；服务器上要跑一次 `npm run build`
  （`web/static/` 是构建产物，已 gitignore）。

## 流水线、成本与三个改造点

链路：输入 → `paper_ingest`（零成本）→ `paper_struct`（花钱）→ LaTeX 工程 →
stage1 幻灯片+光标 → stage2 讲稿 → TTS → 数字人 → 合片。

**成本**（`result/<名字>/token.json` = `{模型: [幻灯片桶, 讲稿桶], "cursor": 秒}`）：
**总量约 4.8 万 ~ 10.9 万 token，讲稿占比 37% ~ 76%**，取决于幻灯片返工次数。
旧记「5.6 万 / 讲稿占 76%」只来自 `result/gkk_en` **一个无返工样本**。

**① `paper_struct.py`**：上限 65536（`PAPER2VIDEO_MAX_TOKENS` 可覆盖）；撞上限**续写**（≤3 轮）
→ 仍不全则**截到最后环境配平点 + 补 `\end{document}`**（实测丢 1.3%、编译通过）。
prompt 写死四条：中文用 `ctex`；表格用 `tabularray` 的 `tblr`（与 ctex 宏包级冲突）；真实论文
标题无样式标记（heading=0）靠语义识别章节；不得编造数据/文献；**不得"顺手纠正"原文**。
保真度 `check_struct_fidelity.py` —— **主指标必须用固定长度窗口**（整句比对假阴性多，
**假警报比不报更糟**）。

**② MuseTalk 偶数尺寸补丁**（`docs/MuseTalk补丁.md`）：上游 `-vf format=yuv420p` 不处理奇数尺寸，
人脸裁框为奇数时 libx264 拒绝编码 → temp.mp4 **0 字节** → **整批页"跑完但无产物"**，而上游照样
打印 result is save。已加 `crop=trunc(iw/2)*2:trunc(ih/2)*2`（源码里要用**转义双引号**）。
自检 `verify_musetalk_patch.py`。

**③ stdout 兜底 + 日志编码**：上游 4 处 print 带 emoji，GBK stdout 下抛 UnicodeEncodeError 且
崩点在**流水线最末端** → `pv_env.py` 模块级 `stdout/stderr.reconfigure(errors="replace")`
（被 9 个模块导入）。`run.log` 同时是父进程写入目标与子进程 stdout 目标，两种编码混写 ⇒
**任何单一解码器都读不了**，表现为界面乱码 + **日志解析正则命中 0 条**（不报错）→ 子进程加
`PYTHONIOENCODING=utf-8`、读取端 `_read_log_text()` 逐行回落（**迁 Linux 后别删这行**）。

架构：FastAPI + 单 worker 串行队列（单卡并行必 OOM）；走子进程 `Popen(argv, cwd=绝对路径)`
不经 shell；只监听 127.0.0.1。

## 开关与配置

- **只需一组 key**：`OPENAI_API_KEY` / `OPENAI_API_BASE`；服务端**按任务注入** `_JOB_KEYS[job_id]`
  （原先是进程级 `_SESSION_KEYS` ⇒ 多人共用一份）。
- **入口是 LaTeX 源码目录**：`--paper_latex_root` 下必须有 `main.tex`。
- **`--if_tree_search`：界面可开、默认关、在产物指纹里**。它是"幻灯片布局微调"（挑 Overfull 溢出的
  页，每页 4 个宽度候选拼 2×2 图，VLM 选一个 → 宽度 4、深度 1）。默认关是**成本**考虑，不是"会崩"
  （早先记的 NameError 来自上游 `else` 分支引用被注释掉的 `latex_code_gen`，**已被我们合并**）。
  文案/默认值由 `/api/options.toggles` 下发。
  ⚠️ **它曾在本项目里结构性空转**（成因已修）：门槛是"Overfull 溢出的页 **且** 该 frame 含
  `\includegraphics`"，而我们喂给模型的工程里**没有任何图片文件**。
  **判据不是"产物变没变"**：`improve_layout` **总会**无条件重写 `slides_refined.tex`，
  要看 **`result/latex_proj/proposal_imgs/` 是否非空**。

## 论文配图提取（`scripts/paper_figures.py`，2026-09-19）

上游入口是「**带图的** LaTeX 工程」；我们从 PDF/Word 出发只提正文 ⇒ 工程里只有 main.tex ⇒
幻灯片 0 张 `\includegraphics` ⇒ **树搜索恒为空转**。现在：源文件提图 → `<工程>/figures/` →
清单附进 slides 提示词。

**正反实证**：`result/zeyu`（上游论文自带工程）→ 8 次 `\includegraphics`、`proposal_imgs/` 5 项、
树搜索 **7 次 VLM 调用 / 29912 token**；我们的 PDF 链路 → 0 次 / 空 / 0 次。

七条硬约束（`docs/论文配图提取.md`）：
1. **提图只在 `pipeline.py` stage 1 内、`copy_folder` 之后**做一次 —— 写在 web 层会与"模型读的是
   副本"漂移（`copy_folder` 只在目标不存在时复制），又是两个读取者。
2. **格式一律转 PNG/JPEG**：期刊 PDF 常见 `.jpx`，XeTeX 的 `\includegraphics` **不支持**它；
   判据是**文件头**不是扩展名；有损源用 JPEG（比 PNG 小 3 倍）。
3. **文件名纯 ASCII**；语义（图题/页号）进 `index.json`。
4. **没找到图就什么都不留** —— 留了会改变"工程文件清单"⇒ 纯文字论文的 deck 指纹变化、白花钱。
5. **幂等判据含源指纹**（version + source_sha1），否则换源会错误跳过且不报错。
6. **"工程自带图" = `\includegraphics` 真正引用的文件**，不是"目录里有图片" —— 按扩展名扫会把
   `main.pdf` 当成图，纯文字论文被误判成有图。
7. `FIGURES_VERSION` 参与 `DECK_FIELDS`（**不进 STRUCT_FIELDS**）；改阈值/命名/筛选必须 +1。

阈值（实测标定）：占页 ≥3%、源短边 ≥150px、按内容 sha1 去重、上限 12 张、最长边 ≤2400px；
矢量簇走 `cluster_drawings()` 兜底但**阈值未标定**（样本里 0 簇），保守 + 记 `kind=vector`。

## 输入与产物：内容寻址

- **上传内容库** `jobs/_content/<sha1[:12]>/`。**解析上传 id 的唯一入口 `_upload_work()`**；
  校验「一个路径段 + 不以点开头」，**不是前缀白名单**（挡不住 `content_../..`，却会误伤手工目录）。
  命中判据 = 内容 sha1 + `paper_ingest.EXTRACTOR_VERSION` ⇒ **改了提取逻辑必须把版本号 +1**。
- **产物目录的唯一入口是 `_result_dir(job)`**（优先 `config.result_dir`）：续跑不复制产物。
  **读取侧（产物列表/逐页索引/文件服务/总页数）必须走它** —— 直拼 `<job_dir>/result` 会让续跑
  任务看到空产物，且"续跑的续跑"被 400 拒掉。
- **产物缓存（`artifact_cache.py`）** `jobs/_cache/struct|deck/<指纹>/`。五条硬规矩：
  ① 两类缓存**各有一份写死的字段表**，加字段前问「它会改变产物吗」——少了静默错配、多了白花钱；
  `deck` 含 `if_tree_search` 与 `figures`，**不含人像/音色**（"换头像不废缓存"是特性）；
  ② **键只算一次、由调用方传下去** —— 算两次必然漂移，表现是"永远命中不了且不报错"；
  ③ 输入取不到指纹 ⇒ **既不命中也不写入**（`REQUIRED_FIELDS`）；
  ④ **只增不删**（`plan_gc()` 只读清点；`GET /api/cache`；逃生门 `force_regenerate`）；
  ⑤ 命中会改 `config.stages`/`reuse_script` 并重建 argv ⇒ 收尾**必须把这两个一起落盘**。
  通用判据：**凡"算出来的值既用于查询又用于写入"，就只算一次**；凡"路径/位置"，问"有几处在拼它"。
  尚未做：GPU 侧产物未进缓存；界面未提示"本次命中了缓存"。

## 数字人产物的复用与跳过判据

- **复用只看 `result_dir`**：落点
  `<result_dir>/talking_<backend>/<音频序号>/<参考图主文件名>/merge_video.mp4`，存在即跳过。
  `result_dir` 每任务一份 ⇒ **手动上传相同素材 = 新任务 = 一页都不跳过**（workspace 的 `avatar_id`
  带时间戳，刻意禁止跨运行复用）⇒ **要省数字人那 10 分钟只能用续跑**。
- ✅ **跳过判据已含音频指纹**：原判据只有（页号, 参考图主文件名），而合片函数签名里没有音频参数、
  音轨取自数字人视频自带那条 ⇒ 音频变了而页数/人像名不变时该页被跳过、**旧配音进成片、新 TTS
  静默丢弃**。现每页目录落 `.inputs.json`（参考图 + 音频的 size/sha1），两处跳过点都比对；
  **任何不确定（旧产物没指纹、指纹损坏、输入读不到）一律判为"不能复用"**。

## Web 侧（`docs/Web可视化方案.md`、`docs/前端体验改进方案.md`）

- 输入支持 `.tex/.zip/.docx/.doc/.pdf`；**pdf 噪声最少、优先用**。转换不用 Office/WPS COM
  （会连到使用者的 WPS 并挂死）；`.doc` 走 olefile（有损）；三种格式都靠 LLM 推断章节。
- **阶段与总进度只有一处定义**：`web/app.py` 的 `PHASES`（9 阶段，带权重/有无计数/卡住阈值），
  `/api/options` 下发。总进度由 `_phase_overall_pct` 算且**单调不减**；没有逐页计数的阶段
  **不编造进度**。⚠️ **计数单位随阶段不同（页/条）—— 混用会算出"18 页"这种荒谬值，
  而界面看着正常**（本项目最难发现的一类错）。
- **数字人阶段的进度靠旁路取数**：批量路径是"一个进程跑完 N 页、之后才逐页 report"（实测 14 条
  记录时间戳全为同一秒）⇒ 真在跑的 5~10 分钟里 progress.jsonl 零记录、还会误报「疑似卡住」。
  现由 `talking_gen._monitor_batch_progress` 后台线程轮询预期产物路径。
- **SSE**：帧逻辑抽成 `_sse_frames()`（还因为 **TestClient 对流式响应是缓冲的**，测心跳必须直驱
  生成器，否则挂死）。前端断流**必须重连** —— 原先 `es.onerror = () => es.close()` 禁用了
  EventSource 自带重连，而产物只在 `job.status` 变化时拉一次 ⇒ 断流后触发点永不到来。
  **"单一触发点 + 静默失败"是同一类坏味道**，凡"只在 X 变化时刷新"都要加轮询兜底。
- ⚠️ 三条"能构建但行为不对"：TDesign 主题 CSS 漏引入（`--td-*` 全未定义）、`ConfigProvider`
  只读 `globalConfig`、build 带 `--noCheck` 掩盖类型错误（现为 `tsc -b && vite build`）。
- `GET /api/jobs/{id}/pages` 由服务端拼逐页索引（幻灯片 1-based / 音频 0-based / 数字人多一层
  参考图目录，**前端别自己拼**）。

## 测试（`scripts/smoke_web_api.py`，14 组 / 310 项，零 API 费用）

- **清理只删「进入作用域前 vs 现在」的快照差集**，绝不按名字前缀批量删共享目录（曾因此误删
  使用者正在运行的任务）。测试自己的命名空间（`smoke_`）例外。
- **清理失败必须复核并报告**：`ignore_errors=True` 会吞掉 Windows 上偶发的删除失败，残留会在
  **下一次**运行时造成"命中了上次遗留"的**假失败**。`_final_sweep()` 负责清掉并打印；
  "按规矩保留（有任务引用）"与"删不掉"要分开报。
- 测试写了用户可见的持久化数据（音色缓存、内容库）必须在 finally 清掉。
- 网络路径全用假 client / 缺 key 覆盖 ⇒ 跑测试不消耗配额。

## 音色：MiMo 造参考音频 + 本地克隆（`docs/音色方案.md`）

- 接法是"**云端只造一次音色、逐页仍本地 Qwen3-TTS 克隆**"：全片同一人声；免费期结束/断网/无 key
  都不影响出片。
- `MIMO_API_KEY` 在服务端 `.env`；参考文本写死在 `mimo_voice.REF_TEXTS`（**必须由我们给定**并放
  assistant —— 用 `optimize_text_preview=True` 会让 ref_text 失控，只能回头靠 ASR 转写，
  而 ASR 在本项目里是已知会转错的）。
- **限时免费 ⇒ 必须有回落**：`resolve()` 任何失败都回落内置音色，原因进响应与 run.log。
- **已生成的音色不需要 key**（缓存命中即可用）。缓存 `jobs/_voices/<sha1>.wav|.txt|.json`。
- ⚠️ `pipeline.py` 的 `ref_audio` 与 `ref_text` 曾各自 `or lang[...]` 回落 ⇒ 自定义音频 + 没给文本
  时会把**档位那段的文本**配上去，克隆失真且**不报错**。已改成 ref_text 跟着 ref_audio 走。
