# 百炼 API 模型目录与项目链路适配

核对日期：2026-10-02。模型目录快照时间：2026-10-02T23:11:03+08:00。

## 查询范围与实际结果

使用用户授权的 API Key，对用户指定北京工作空间只执行模型目录 GET 请求，没有发起推理、音色创建、图像检测、媒体上传、配音或视频生成。

| 目录 | 同一工作空间下的请求路径 | 实际结果 |
| --- | --- | --- |
| OpenAI 兼容目录 | `/compatible-mode/v1/models` | HTTP 200；261 条；`has_more=false` |
| 百炼原生目录 | `/api/v1/models?page_no=1&page_size=100&language=zh-CN` | HTTP 200；总计518条；读取6页，100/100/100/100/100/18 |

两个目录不是包含关系：交集228个型号，原生目录独有290个，兼容目录独有33个。兼容目录中也有音频/图像型号，不能把目录可见解释成它们都接受 chat/completions；原生目录也不是所有地域、所有部署类型的完整全集。

不含凭据的完整快照：`docs/bailian-model-catalog-2026-10-02.json`。包含261个兼容型号ID和518个原生型号记录，记录ID、名称、提供方、能力标签、输入输出模态、模型规格、下线信息及是否出现在兼容目录。原生快照不保存长描述、报价或用户凭据。

原生目录能力标签统计（类别重叠，不可相加当成模型总数）：文本TG 154、视觉VU 67、TTS 83、ASR 26、实时ASR 15、视频VG 89、图像IG 62。还有推理、多模态、嵌入、排序、翻译等类别。

鉴权结论仅限目录读取成功，尚未验证每个型号的推理权限、余额、限流、音色权限或媒体生成效果。

## 推荐的 API 模式首版组合

以下是根据目录状态和官方接口能力选择的候选，不是已实现、已验证或效果最优的宣称。

| 环节 | API 候选 | 现有本地路线与注意事项 |
| --- | --- | --- |
| 材料结构化、课件与编译修复 | `qwen3.8-flash`；复杂任务候选 `qwen3.8-max` | 文本仍可使用本地兼容模型服务；不新造本地权重加载实现 |
| 看课件生成讲稿、焦点描述 | `qwen3-vl-plus`；可评估 `qwen3.8-flash` | API模式保持逐页材料、讲稿与审核契约 |
| 光标空间定位 | `qwen3-vl-plus`；另评估 `gui-plus` | 保留GUI-Owl；按模型单独解析和归一化坐标，不混用999/1000或像素坐标 |
| 逐页配音 | `cosyvoice-v3.5-plus`，开启流式回调及字级时间戳 | 保留Qwen3-TTS；云端同时保存音频和时间戳，不只保存wav |
| 上传录音克隆音色 | `voice-enrollment`，target_model=`cosyvoice-v3.5-plus` | 保留本地参考音色克隆；云端音色ID绑定提供方与目标模型 |
| 按文字设计音色 | `voice-enrollment`的声音设计流程，target_model=`cosyvoice-v3.5-plus` | 保留MiMo现有音色入口；是否新增本地设计路线留待实现时明确 |
| 参考音频转写 | `fun-asr`或`qwen-audio-3.1-asr-flash-filetrans` | 保留Qwen3-ASR；已提供参考文本时仍跳过此阶段 |
| 配音字词时间轴 | 使用CosyVoice合成时返回的字级时间戳 | API模式不需要为这段新生成音频加载ForcedAligner；本地模式继续用现有ForcedAligner |
| 数字人 | `wan2.2-s2v-detect` + `wan2.2-s2v` | 保留MuseTalk；云端音频必须切成每段小于20秒，异步生成后拼接与音轨检查 |

模型目录内未找到ID/名称匹配ForcedAligner或强制对齐的专用条目。这个事实不等于完全无法做云端时间轴：本项目主要对齐自己刚合成的讲稿音频，CosyVoice可以在合成阶段提供时间信息，无需先重新识别再猜测文字对应关系。

## 最新核实：配音与时间轴可以合并

官方CosyVoice Python SDK文档明确说明：

- `word_timestamp_enabled`默认false，需通过`additional_params`开启。
- 仅在流式输出模式可用；设置callback后音频通过`on_data`返回，事件提供句子与words信息。
- 支持CosyVoice v3.5-plus/v3.5-flash/v3-flash/v3-plus/v2的复刻音色，以及音色列表中标记支持的系统音色。不是所有内置音色均支持。
- 事件包含sentence-begin/sentence-synthesis/sentence-end，相关事件可带original_text及sentence.words。

因此首版可选兼容时间戳的系统音色或复刻音色，将音频与时间戳同批落盘，再适配现有`segments[].words[]`的word/start/end结构，并处理句子/分块偏移和文字规范化。不能在时间戳缺失时悄悄改成本地推理或按字数均分冒充精确时间轴。

限制：这不是对任意用户已有音频做指定文本强制对齐的通用API。如果以后支持上传完整配音并强制贴合指定讲稿，仍需另选远程对齐接口，或经用户接受使用ASR时间戳加文本匹配策略。

本段修正此前仅考虑“独立ForcedAligner API或ASR时间戳”的不完整判断；不是宣称已通过运行测试。

## 下线风险与上一轮建议修正

本次真实目录的`inference_offline_info.offline_time`标注以下型号在2026-10-10 00:00:00下线（目录原值未附时区，不作额外时区推断）：

- `liveportrait`、`liveportrait-detect`。
- `qwen3-vl-flash`。
- `qwen3-tts-vc-2026-01-22`、`qwen3-tts-vd-2026-01-26`。
- `qwen-voice-design`。

它们仍可能出现在目录或历史文档，但不适合作为新接入默认型号。上一轮提到LivePortrait具有原生能力属实，新增接入推荐应调整为当前目录未标注下线的Wan2.2-S2V路线。该路线单次音频小于20秒，不可继承LivePortrait小于3分钟的限制。

`deepseek-flash`没有出现在本次两个目录中，不应直接保留项目模板的该默认名称作为百炼默认。`qwen3.5-omni-plus`在目录中，但它是Omni路线，不因可列出就自动成为课件视觉、TTS、ASR和数字人统一替代模型。

## 后端接入必须区分协议

- 用户提供的`compatible-mode/v1`地址用于符合兼容协议的模型调用；不能把所有语音、音色和数字人请求发往chat/completions。
- 同一工作空间下原生`/api/v1/models`已可读取，后续原生推理URL、WebSocket入口和SDK的地域/工作空间配置需按对应文档明确设置。不要默认SDK自动使用这次工作空间的域名。
- 配音首版采用CosyVoice流式API以拿到真实时间戳；不需要浏览器播放流式音频，后端可接收并存成完整文件后继续流程。
- 数字人要检测图片、提供可访问的媒体URL、提交任务、查询状态、下载结果、分段合并。Wan文档说明生成时长跟随输入音频，单段小于20秒，输出URL有时效。
- 全API模式不加载本地TTS/ASR/ForcedAligner/GUI-Owl/MuseTalk，失败须显式报错。已有本地模式保持独立入口。
- 前端提供快捷预设与分环节选择，音色选项按模型/时间戳能力过滤，下线型号默认不推荐，切换时保存任务配置快照和隔离缓存。
- API Key不保存到目录快照、文档、日志、job.json或Git，不回显给前端；本次没有改写实际`.env`。
- LaTeX/PDF/FFmpeg、队列、缓存和审核是非模型步骤，不需要为了“全API模型推理”迁移。

## 官方依据

- CosyVoice时间戳与流式回调：https://help.aliyun.com/zh/model-studio/cosyvoice-python-sdk
- 声音复刻：https://help.aliyun.com/zh/model-studio/voice-cloning-user-guide
- 声音设计：https://help.aliyun.com/zh/model-studio/voice-design-user-guide
- 模型与声音能力：https://help.aliyun.com/zh/model-studio/tts-model
- 转写时间戳：https://help.aliyun.com/zh/model-studio/non-realtime-speech-recognition-user-guide
- 视觉定位：https://help.aliyun.com/zh/model-studio/vision
- Wan数字人概述：https://help.aliyun.com/zh/model-studio/wan-s2v-overview
- Wan数字人API：https://help.aliyun.com/zh/model-studio/wan-s2v-api

本次只完成实际目录查询、文档核对与JSON结构检查，不把候选可见性当作推理成功或端到端验收。
