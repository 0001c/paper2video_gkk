# paper2video 项目长期笔记

## 项目定位与约定

本仓库是「论文研究 / 复现」工作区，不是上游仓库的 fork。

- `Paper2Video-main/`：上游开源代码包（showlab/Paper2Video，即论文官方实现）。
  ⚠️ **它在 Git 里**（2026-09-19 核实：`src/` 下 172 个文件被跟踪，`pipeline.py`
  自己有 11 次提交；`.gitignore` 只忽略它的构建产物与 `__pycache__`）。
  早先笔记写的"已忽略、不要修改、不要提交"是**错的** —— 那会让人以为补丁不在
  版本控制里。**我们确实会改它**（补丁总账见 `docs/上游补丁清单.md`），
  改动必须同时落到 `docs/` 与提交信息里。**唯一不在本仓库 Git 里的是 MuseTalk**
  （独立仓库）——"重装后必须重打"只对它成立。
- `docs/` 归档论文与分析结论；`scripts/` 本仓库自己的脚本；`.venv/` 虚拟环境（根目录）。
- Python 一律走 `.venv`。分析结论写进 `docs/`，不回写上游目录。

**关键事实**：论文 = arXiv 2510.05096v2（Show Lab, NUS）。benchmark 数据在
HuggingFace `ZaynZhu/Paper2Video`；数字人权重需另装（见模型选型）。

## 模型选型（2026-09-17 定，详见 `docs/模型替换方案.md`）

用户目标：本地模型国产化（可用 > 效果），API 侧全用 DeepSeek-V4.1-Flash。

- TTS → `Qwen/Qwen3-TTS-12Hz-1.7B-Base`（Apache-2.0，替 F5-TTS 的 CC-BY-NC）
- 光标 → `mPLUG/GUI-Owl-1.5-4B-Instruct`（MIT；UI-TARS-7B 装不下）
- 转写对齐 → `Qwen3-ASR-0.6B` + `Qwen3-ForcedAligner-0.6B`
- 数字人 → `MeiGen-AI/InfiniteTalk`（Apache-2.0；Hallo2 锁 cu118，sm_120 上是死结）
- API 语言+视觉 → `deepseek-flash`；API 视频理解 → `Qwen3.5-Omni-Plus`

**省返工的两条**：① `wei_utils.get_agent_config()` 有兜底分支 ⇒ 换模型只需设
`OPENAI_API_BASE/KEY` + `--model_name_t deepseek-flash`，**不用改代码**；
② `cursor_gen.py:55` 本就用 `factor=1000, model_type="qwen25vl"`，与 GUI-Owl 同源。
**硬约束**：单卡按阶段加载/卸载；路径含中文 ⇒ 权重放英文目录；`HF_HOME` 在 E/F 盘。
**许可证雷区**：`tencent/HunyuanVideo-Avatar` 非商用 ⇒ 商业交付只用 InfiniteTalk。

## 迁移目标：学校 Linux 服务器（本机 Windows 只是开发环境）

开发机上永远不出现、但 Linux 上必现的问题：

- 中文字幕字体**不能硬编码 Windows 字体**（已修：`_cjk_subtitle_font()`，
  可用 `PAPER2VIDEO_CJK_FONT` 覆盖）—— 英文正常、中文变方框。
- 权重/缓存路径不要出现盘符；GPU 档位不要写死（服务器可能多卡）。
- **平台编码**：Windows 上 `open()` 不带 encoding 用 **cp936**，Linux 上是 UTF-8。
  我们链路的 11 处已显式 `encoding="utf-8"`，且流水线子进程环境加了
  **`PYTHONUTF8=1`**（`web/app.py::_run_job`、`scripts/gkk_3combos.py`）——
  后者**推翻了早先"只设 PYTHONIOENCODING"的决定**（旧理由在"上游 92 处 open() 假设
  UTF-8"面前不成立）。自检 `scripts/verify_tex_encoding.py` + 冒烟第 13 组。
  ⚠️ 判据要记住：**编译/命令退出码 0 ≠ 内容正确** —— GBK 写出的 .tex 让 tectonic
  返回 0 却把非 ASCII 全替换成 U+FFFD（实测 PDF 里中文三个判据全 False）。
- **MuseTalk 补丁改的是上游仓库、不在本仓库 Git 里 → 重装后必须重打**。
- 多人使用要补：任务按人隔离（凭据已按 job 隔离）、排队可见、按人统计 token、只监听内网。
- 跑冒烟测试要 `CODEBUDDY_SAFE_DELETE_ENABLED=0`（沙箱批量删除守卫会拦测试自己的清理）；
  服务器上装 node 跑一次 `npm run build`（`web/static/` 是构建产物，已 gitignore）。

## 流水线与三个必知的改造点

链路：输入 → `paper_ingest` 提取（零成本）→ `paper_struct` LLM 结构化（花钱）→
LaTeX 工程 → stage1 幻灯片+光标 → stage2 讲稿 → TTS → 数字人 → 合片。
成本口径 `result/<名字>/token.json`：一篇约 5.6 万 token，**讲稿占 76%**。

**① 结构化层（`scripts/paper_struct.py`）**：输出上限 65536
（`PAPER2VIDEO_MAX_TOKENS` 可覆盖）。撞上限会**续写**（≤3 轮）→ 仍不全则**截到最后
环境配平点 + 补 `\end{document}`**（实测丢 1.3%、编译通过）。prompt 必须写死四条：
中文用 `ctex`、表格用 `tabularray` 的 `tblr`（与 ctex 宏包级冲突）；真实论文标题无样式
标记（heading=0）靠语义识别章节；不得编造数据/文献；**不得"顺手纠正"原文**。
保真度检查 `scripts/check_struct_fidelity.py` —— **主指标必须用固定长度窗口**，
整句比对会产出大量假阴性，而**假警报比不报更糟**。

**② MuseTalk 偶数尺寸补丁**（`docs/MuseTalk补丁.md`）：上游 `-vf format=yuv420p`
不处理奇数尺寸，人脸裁框为奇数（如 215×221）时 libx264 拒绝编码 → temp.mp4 = 0 字节
→ **整批页"跑完但无产物"**，而上游照样打印 result is save、耗时正常。
已加 `crop=trunc(iw/2)*2:trunc(ih/2)*2`；源码里要用**转义双引号**（f-string 定界符）。
自检 `python scripts/verify_musetalk_patch.py`；`talking_gen` 批量入口也会提示。

**③ stdout 兜底 + 日志编码**：上游 4 处 print 带 emoji（`\u2705`/`\u26a0`），
GBK stdout 下抛 UnicodeEncodeError 且崩点在**流水线最末端** → `pv_env.py` 模块级
`stdout/stderr.reconfigure(errors="replace")`（被 9 个模块导入）。`run.log` 同时是
父进程写入目标与子进程 stdout 目标，两种编码混写 ⇒ **任何单一解码器都读不了**，
表现为界面乱码 + **日志解析正则命中 0 条**（不报错）→ 子进程加
`PYTHONIOENCODING=utf-8`、读取端 `_read_log_text()` 逐行回落（**迁 Linux 后别删这行**）。

架构：FastAPI + 单 worker 串行队列（单卡并行必 OOM）；流水线走子进程
`Popen(argv, cwd=绝对路径)` 不经 shell；只监听 127.0.0.1。

## 开关与配置

- **只需一组 API key**：`OPENAI_API_KEY` / `OPENAI_API_BASE`。前端存 localStorage，
  服务端**按任务注入** `_JOB_KEYS[job_id]`（原先是进程级 `_SESSION_KEYS` ⇒ 多人共用
  一份、最后输入的人覆盖所有人）。回落 .env 时必须写 run.log。
- **入口是 LaTeX 源码目录**：`--paper_latex_root` 下必须有 `main.tex`。
- **`--if_tree_search`：界面上可开、默认关，且在产物指纹里**（2026-09-19）。
  它是"幻灯片布局微调"（编译后挑出 Overfull 溢出的页，每页 4 个宽度候选拼 2×2 图，
  VLM 选一个 → 宽度 4、深度 1 的束搜索）。默认关是**成本**考虑，不是"会崩"：早先记的
  NameError 来自上游 `pipeline.py` 的 `else` 分支引用了被注释掉的 `latex_code_gen`，
  **该分支已被我们合并**（symtable 复核 `improve_layout` 调用链无未定义引用）。
  ⚠️ 它改变幻灯片产物 ⇒ **必须在 `artifact_cache.DECK_FIELDS` 里**，且值要归一成 bool。
  开关文案/默认值/成本口径由 `/api/options.toggles` 下发，前端不硬编码。
  ⚠️ **实测（2026-09-19 11:20）它在本项目里结构性空转**（详见 `docs/树搜索实测.md`）：
  入选门槛是"Overfull 溢出的页 **且** 该 frame 含 `\includegraphics`"，而我们喂给模型的
  工程里**没有任何图片文件**（`paper_ingest → paper_struct` 只提正文，`latex_proj/` 里
  只有 main.tex+main.pdf），中文提示词又明确授权"若没有可用图片就不要写 `\includegraphics`"
  ⇒ 历史上 3 份 slides.tex 的 `\includegraphics` **全是 0** ⇒ `need_improve_list` 恒为空。
  判据不是"产物变没变"：`improve_layout` **总会**重写一份 `slides_refined.tex`（无条件），
  要看 **`result/latex_proj/proposal_imgs/` 是否非空**（空 = 一页都没改进）。
  ⇒ 想让论文这个核心机制真跑起来，必须先让**图片进入 `paper_latex_root`**。

## 输入与产物：内容寻址

**上传内容库**：`jobs/_content/<sha1[:12]>/` = `meta.json` + 不可变的
`input/<sha1><ext>` + 派生 `ingest/`。**解析上传 id 的唯一入口 `_upload_work()`**；
校验的是「一个路径段 + 不以点开头」，**不是前缀白名单**（白名单挡不住
`content_../..`，却会误伤手工建的目录）。命中判据 = 内容 sha1 +
`paper_ingest.EXTRACTOR_VERSION` ⇒ **改了提取逻辑必须把版本号 +1**（否则命中旧缓存
且不报错）。派生正文仍按任务复制到 `<job_dir>/ingest/`（任务自包含）。
老目录（`ingest_*`/`portrait_*`）**不迁移**：可回收仅约 1.2 MB，不值得动历史记录。

**产物目录的唯一入口是 `_result_dir(job)`**（优先 `config.result_dir`）：续跑不复制
产物，新任务指过去。**读取侧（产物列表/逐页索引/文件服务/总页数）必须走它** ——
直拼 `<job_dir>/result` 会让续跑任务看到空产物，且"续跑的续跑"被 400 拒掉。

**产物缓存（阶段 3，`scripts/artifact_cache.py`）**：`jobs/_cache/struct|deck/<指纹>/`，
命中即跳过**唯一两步联网付费**的步骤。五条硬规矩：

1. **两类缓存各有一份写死的字段表**：加字段前问「它会改变产物吗」。少了 → 输入不同却
   命中（静默出错）；多了 → 白花钱。`struct` 不含语言/人像/音色，`deck` 不含人像/音色
   （"换头像不废缓存"是**特性**），但**含 `if_tree_search`**。
2. **键只算一次、由调用方传下去**（`lookup_*`/`publish_*` 都收 `key=`）。算两次必然漂移
   （流水线会改写 `main.tex`），漂移的表现是"永远命中不了且不报错"。通用判据：
   **凡"算出来的值既用于查询又用于写入"，就只算一次**。
3. **输入取不到指纹 ⇒ 既不命中也不写入**（`REQUIRED_FIELDS`），否则不同输入会因为
   字段都是 None 而塌成同一个键。
4. **只增不删**：模块里没有删除实现，`plan_gc()` 只读清点；`GET /api/cache` 查看；
   逃生门 `config.force_regenerate=True`。
5. ⚠️ 命中会改 `config.stages`/`reuse_script` 并重建 argv ⇒ 收尾**必须把这两个一起落盘**。

**还没做**：GPU 侧产物（TTS/光标/数字人/成片）未进缓存；界面未提示"本次命中了缓存"。

## 数字人产物的复用与跳过判据

- **复用只看 `result_dir`**：落点
  `<result_dir>/talking_<backend>/<音频序号>/<参考图主文件名>/merge_video.mp4`，
  存在即跳过。`result_dir` 每任务一份 ⇒ **手动上传相同素材 = 新任务 = 一页都不跳过**
  （中间 workspace 的 `avatar_id` 带时间戳，刻意禁止跨运行复用）；
  `_file_sha1()` 只在**单次运行内**按参考图分组，不是跨运行去重。
- ✅ **跳过判据已含音频指纹**（2026-09-19 修）：原先判据只有（页号, 参考图主文件名），
  而合片函数签名里没有音频参数、音轨取自数字人视频自带那条 ⇒ 音频变了而页数/人像名不变时
  该页被跳过、**旧配音进成片、新 TTS 音频静默丢弃**，全程不报错。
  现每页目录落 `.inputs.json`（参考图 + 音频的 size/sha1），两处跳过点都比对；
  **任何不确定（旧产物没指纹、指纹损坏、输入读不到）一律判为"不能复用"**。
- **要省数字人那 10 分钟只能用续跑**，不能靠重新上传（见上）。

## Web 侧（`docs/Web可视化方案.md`、`docs/前端体验改进方案.md`）

- 输入支持 `.tex/.zip/.docx/.doc/.pdf`；**pdf 噪声最少、优先用**。转换层不用
  Office/WPS COM（会连到使用者的 WPS 并挂死）；`.doc` 走 olefile（有损）；
  真实 Word 不用标题样式（heading=0）⇒ `.docx` 无结构优势，三种格式都靠 LLM 推断章节。
- **阶段与总进度只有一处定义**：`web/app.py` 的 `PHASES`（9 阶段，带权重/有无计数/
  卡住阈值），`/api/options` 下发渲染步骤条；总进度由后端 `_phase_overall_pct` 算且
  **单调不减**；没有逐页计数的阶段**不编造进度**，改用"已用 / 约"。计数单位随阶段不同
  （页/条）—— 混用会算出"18 页"这种荒谬值且界面看着正常。
- **数字人阶段的进度靠旁路取数**：批量路径是"一个进程跑完 N 页、之后才逐页 report"
  （实测 14 条记录时间戳全为同一秒）⇒ 真正在跑的 5~10 分钟里 progress.jsonl 零记录、
  条停 100%、还会误报「疑似卡住」。现由 `talking_gen._monitor_batch_progress` 后台线程
  轮询预期产物路径（**不改 MuseTalk**），旧文件靠 `mtime + 1.0 >= started` 排除。
- **SSE**：帧逻辑抽成 `_sse_frames()`（心跳注释帧、间隔可用 `PAPER2VIDEO_SSE_KEEPALIVE`
  覆盖）。抽出来还有第二个原因：**TestClient 对流式响应是缓冲的**，测心跳必须直驱生成器。
  前端断流必须重连并提示 —— 原先 `es.onerror = () => es.close()` **禁用了 EventSource
  自带重连**，而产物只在 `job.status` 变化时拉一次 ⇒ 断流后唯一触发点永不到来。
  **"单一触发点 + 静默失败"是同一类坏味道**，凡"只在 X 变化时刷新"都要加轮询兜底。
- ⚠️ 三条"能构建但行为不对"的坑：TDesign 主题 CSS 漏引入（`--td-*` 全未定义）、
  `ConfigProvider` 只读 `globalConfig`（locale 被忽略）、build 带 `--noCheck` 掩盖类型
  错误。现 build 是 `tsc -b && vite build`。
- `GET /api/jobs/{id}/pages` 由服务端拼逐页索引（幻灯片 1-based / 音频 0-based /
  数字人多一层参考图目录，命名不一致，**前端别自己拼**）。

## 测试（`scripts/smoke_web_api.py`，273 项断言，零 API 费用）

- **清理只删「进入作用域前 vs 现在」的快照差集**，绝不按名字前缀批量删共享目录
  （曾因此误删使用者正在运行的任务）。测试自己的命名空间（`smoke_`）例外。
- **清理失败必须复核并报告**：`ignore_errors=True` 会吞掉 Windows 上偶发的删除失败，
  残留会在**下一次**运行时造成"命中了上次遗留"的**假失败**。`main()` 末尾的
  `_final_sweep()` 负责清掉并打印；"按规矩保留（有任务引用）"与"删不掉"要分开报。
- 测试写了用户可见的持久化数据（音色缓存、内容库）必须在 finally 清掉。
- 网络路径全用假 client / 缺 key 覆盖 ⇒ 跑测试不消耗配额。

## 音色：MiMo 造参考音频 + 本地克隆（`docs/音色方案.md`）

- **接法是"云端只造一次音色、逐页仍本地 Qwen3-TTS 克隆"**（不是逐页调云端 TTS）：
  全片同一人声；免费期结束/断网/无 key 都不影响出片。地基已实测（f0 差 53 Hz）。
- `MIMO_API_KEY` 在服务端 `.env`；参考文本写死在 `mimo_voice.REF_TEXTS`（**必须由我们
  给定**并放 assistant —— 用 `optimize_text_preview=True` 会让 ref_text 失控，只能回头靠
  ASR 转写，而 ASR 在本项目里是已知会转错的）。
- **限时免费 ⇒ 必须有回落**：`resolve()` 任何失败都回落内置音色，原因进响应与 run.log。
- **已生成的音色不需要 key**（缓存命中即可用）⇒ 攒下的音色资产长期有效。
  缓存 `jobs/_voices/<sha1>.wav|.txt|.json`，key = kind|voice|prompt|lang。
- ⚠️ `pipeline.py` 的 `ref_audio` 与 `ref_text` 曾各自 `or lang[...]` 回落 ⇒ 自定义音频 +
  没给文本时会把**档位那段的文本**配上去，克隆失真且**不报错**。已改成 ref_text 跟着
  ref_audio 走（无文本则保持 None 交给 ASR）。
- 预置音色 8 个（中英男女各 2）；**不用 `mimo_default`**（因集群而异，同一份配置在不同
  部署上会出不同音色）。
