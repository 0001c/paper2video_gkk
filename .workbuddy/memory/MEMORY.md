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

**已定选型**（许可证/参数量均在 HF API 上实测过）：

- 语音合成：F5-TTS（CC-BY-NC **禁商用**）→ `Qwen/Qwen3-TTS-12Hz-1.7B-Base`（Apache-2.0, 1.93B）
- 光标定位：UI-TARS-1.5-7B（16–18GB 装不下）→ `mPLUG/GUI-Owl-1.5-4B-Instruct`（MIT, 4.44B）
- 转写对齐：WhisperX → `Qwen/Qwen3-ASR-0.6B` + `Qwen/Qwen3-ForcedAligner-0.6B`
- 数字人：Hallo2（锁 cu118，sm_120 死结）→ `MeiGen-AI/InfiniteTalk`（Apache-2.0, 8GB）
- 音色相似度：SpeechBrain ECAPA → CAM++（3D-Speaker / ModelScope `iic/...`）
- API 语言+视觉：GPT-4.1 / Gemini-2.5-Pro → **`deepseek-flash`**（原生图像输入、1M 上下文）
- API 视频理解：Gemini-2.5-Flash → `Qwen3.5-Omni-Plus`（DeepSeek 不支持视频输入）

**改造时先记住三条**（能省大量返工）：

1. `src/wei_utils.py:get_agent_config()` 末尾有兜底分支 → 未在白名单的模型名走
   `OPENAI_COMPATIBLE_MODEL`，**换 DeepSeek 只需设 `OPENAI_API_BASE`/`OPENAI_API_KEY` +
   传 `--model_name_t deepseek-flash`，无需改代码**。
2. `cursor_gen.py:55` 本就用 `parse_action_to_structure_output(factor=1000, model_type="qwen25vl")`，
   与 GUI-Owl 的 0–1000 归一化坐标同源 → 换模型近乎零改造。
3. 单卡必须放弃 `multiprocessing.Pool` 多进程（每 worker 各持一份权重 ≈24GB+），
   改为**按阶段加载/卸载**；`infer_cursor()` 的 pipeline 要提到模块级（原来每句重载一次）。

**两个环境硬约束**：① 项目路径含中文，而 InfiniteTalk 要求纯英文路径 → 权重/输出放英文目录；
② `HF_HOME` 必须改到 E/F 盘（C 盘只剩 24GB）。

**许可证雷区**：`OmniAvatar-1.3B` 的 model card 无 license 字段；
`tencent/HunyuanVideo-Avatar` 是腾讯社区协议（非商用）→ 商业交付只用 Apache-2.0 的 InfiniteTalk。
