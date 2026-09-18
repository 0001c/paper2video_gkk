"""语音合成与转写。

替换说明（2026-09，详见 docs/模型替换方案.md）
------------------------------------------------
原实现                          现实现                          许可证变化
F5-TTS 0.34B                  Qwen3-TTS-12Hz-1.7B-Base        CC-BY-NC -> Apache-2.0
whisperx large-v2/large-v3    Qwen3-ASR-0.6B                  BSD-4   -> Apache-2.0
whisperx.align                Qwen3-ForcedAligner-0.6B        BSD-4   -> Apache-2.0

关键实现细节
------------
1. **模型一律惰性单例**。原代码 ``inference_f5`` 每次调用都 ``F5TTS()``，
   在逐页循环里等于每页重载一次模型；Qwen3-TTS 权重 3.9 GB，重载代价更高。
2. **参考音频的音色 prompt 只编码一次**。``create_voice_clone_prompt`` 的结果
   被缓存复用，避免逐页重复编码 ref_audio。
3. **不使用 flash-attention**。Windows 上 flash-attn 几乎无法编译安装，
   统一走 PyTorch 自带的 SDPA，精度无损失、速度差异在小模型上可忽略。
4. 旧实现（F5-TTS / WhisperX）保留为可选分支，未安装对应依赖时会给出明确提示，
   不影响 Qwen3 路径。
"""

import os
import re
import shutil
from os import path

import pv_env

# 结构化进度（P1）：给 Web 端一份机器可读的进度，免得它去正则匹配日志文本。
# 默认不生效（未设置 PAPER2VIDEO_PROGRESS_FILE 时 report() 直接 return），
# 且这里再兜一层 —— 进度上报是可选增强，导入失败绝不能拖垮流水线。
try:
    import progress_report
except Exception:                      # pragma: no cover
    progress_report = None

# ── HuggingFace repo id（实际路径由 pv_env.model_path 解析）──
TTS_REPO = "Qwen/Qwen3-TTS-12Hz-1.7B-Base"
ASR_REPO = "Qwen/Qwen3-ASR-0.6B"
ALIGNER_REPO = "Qwen/Qwen3-ForcedAligner-0.6B"

# ── 惰性单例 ──
_tts_model = None
_tts_prompts = {}          # (ref_audio, ref_text) -> voice clone prompt
_asr_model = None
_aligner_model = None


def _device():
    import torch
    if torch.cuda.is_available():
        return "cuda:0"
    return "cpu"


def _dtype():
    import torch
    return torch.bfloat16 if _device().startswith("cuda") else torch.float32


def get_tts_model():
    """加载 Qwen3-TTS（零样本音色克隆分支）。"""
    global _tts_model
    if _tts_model is None:
        import torch
        from qwen_tts import Qwen3TTSModel
        name = pv_env.model_path(TTS_REPO)
        print("[speech_gen] 加载 Qwen3-TTS: {} ({})".format(name, _device()))
        _tts_model = Qwen3TTSModel.from_pretrained(
            name, device_map=_device(), dtype=_dtype(),
        )
    return _tts_model


def get_clone_prompt(ref_audio, ref_text):
    """编码参考音频得到可复用的音色 prompt（同一参考音频只算一次）。"""
    key = (str(ref_audio), ref_text or "")
    if key not in _tts_prompts:
        print("[speech_gen] 编码参考音色: {}".format(ref_audio))
        _tts_prompts[key] = get_tts_model().create_voice_clone_prompt(
            ref_audio=str(ref_audio), ref_text=ref_text, x_vector_only_mode=False,
        )
    return _tts_prompts[key]


def get_asr_model():
    """加载 Qwen3-ASR（转写）。"""
    global _asr_model
    if _asr_model is None:
        import torch
        from qwen_asr import Qwen3ASRModel
        name = pv_env.model_path(ASR_REPO)
        print("[speech_gen] 加载 Qwen3-ASR: {}".format(name))
        _asr_model = Qwen3ASRModel.from_pretrained(
            name, dtype=_dtype(), device_map=_device(),
            max_inference_batch_size=8, max_new_tokens=1024,
        )
    return _asr_model


def get_aligner_model():
    """加载 Qwen3-ForcedAligner（词级时间戳）。"""
    global _aligner_model
    if _aligner_model is None:
        import torch
        from qwen_asr import Qwen3ForcedAligner
        name = pv_env.model_path(ALIGNER_REPO)
        print("[speech_gen] 加载 Qwen3-ForcedAligner: {}".format(name))
        _aligner_model = Qwen3ForcedAligner.from_pretrained(
            name, dtype=_dtype(), device_map=_device(),
        )
    return _aligner_model


def release_models(which=("tts", "asr", "aligner")):
    """按阶段释放显存。单卡 16 GB 上串行执行各阶段时必要。

    例：TTS 跑完、进入光标定位前先 ``release_models(("tts",))``，
    腾出约 5 GB 给 GUI-Owl。
    """
    global _tts_model, _asr_model, _aligner_model
    import torch

    if "tts" in which and _tts_model is not None:
        del _tts_model
        _tts_model = None
        _tts_prompts.clear()
    if "asr" in which and _asr_model is not None:
        del _asr_model
        _asr_model = None
    if "aligner" in which and _aligner_model is not None:
        del _aligner_model
        _aligner_model = None

    if torch.cuda.is_available():
        torch.cuda.empty_cache()
    print("[speech_gen] 已释放: {}".format(", ".join(which)))


# ──────────────────────────────────────────────────────────────
# 转写与对齐
# ──────────────────────────────────────────────────────────────

def transcribe_with_qwen3asr(audio_path, lang=None):
    """Qwen3-ASR 转写，返回纯文本。

    lang 传 ``None`` 走自动语种识别；也可传 ``"English"`` / ``"Chinese"`` 强制。
    """
    results = get_asr_model().transcribe(audio=audio_path, language=lang)
    return results[0].text.strip()


def align_with_qwen3(audio_path, text, lang="English"):
    """Qwen3-ForcedAligner 对齐，返回 **whisperx 兼容** 的结构。

    为什么要适配成 whisperx 的 shape：``cursor_gen.timesteps()`` 按
    ``aligned["segments"][i]["words"][j]["end"]`` 取值。保持这个形状，
    下游的时间轴拼装逻辑一行都不用改。

    返回结构说明（官方示例：``results[0][0].text``）：``align()`` 返回一个
    按输入音频分组的列表，每个元素是该音频的**词条序列**。单条音频时取
    ``results[0]``。这里不靠 ``isinstance(x, list)`` 判断层级 —— 模型侧若
    返回 tuple 之类就会判错，改为**看元素是否具备 ``start_time`` 属性**，
    语义上更准确。
    """
    aligned = get_aligner_model().align(audio=audio_path, text=text, language=lang)
    if aligned is None:
        return {"segments": [{"words": []}]}

    first = aligned[0] if len(aligned) else None
    # first 若有 start_time，说明本层已经是词条；否则它才是词条序列
    items = aligned if hasattr(first, "start_time") else (first or [])

    words = []
    for item in items:
        words.append({
            "word": str(getattr(item, "text", "")),
            "start": float(getattr(item, "start_time", 0.0) or 0.0),
            "end": float(getattr(item, "end_time", 0.0) or 0.0),
        })
    return {"segments": [{"words": words}]}


def transcribe_with_whisperx(audio_path, lang="en", device=None):
    """[已弃用] 原 WhisperX 实现，仅用于对照排查，需要自行安装 whisperx。

    新流程请用 :func:`transcribe_with_qwen3asr`。保留此函数是为了让
    ``pipeline_light.py`` 里旧的调用点在迁移期不至于直接崩掉。
    """
    try:
        import torch
        import whisperx
    except ImportError as exc:
        raise RuntimeError(
            "WhisperX 已从默认依赖中移除（改为 Qwen3-ASR）。"
            "如需回退旧行为，请自行 pip install whisperx。原始报错: {}".format(exc)
        )

    device = device or ("cuda" if torch.cuda.is_available() else "cpu")
    model = whisperx.load_model("large-v2", device=device,
                                compute_type="float16" if device == "cuda" else "int8")
    result = model.transcribe(audio_path, language=lang)
    model_a, metadata = whisperx.load_align_model(language_code=result["language"], device=device)
    result_aligned = whisperx.align(result["segments"], model_a, metadata, audio_path, device)
    return " ".join(seg["text"].strip() for seg in result_aligned["segments"])


# ──────────────────────────────────────────────────────────────
# 语音合成
# ──────────────────────────────────────────────────────────────

# ──────────────────────────────────────────────────────────────
# 退化生成的护栏（2026-09-18）
# ──────────────────────────────────────────────────────────────
# Qwen3-TTS 会**随机陷入超长自回归生成**：实测两次，单页从 ~30 秒退化到 10 分钟
# 以上仍未结束（CPU 单核满载、GPU ~34%）。卡住那页只有 125 汉字，比已经成功的那页
# 还短 —— 所以这**与文本内容无关**，是采样层面的偶发退化，改文本救不了，只能加护栏。
#
# generate_voice_clone 接受 max_new_tokens 并透传到底层 generate（实测：
# 80 -> 6.32s、400 -> 14.64s），而 Qwen3-TTS 是 **12Hz** 编码，
# 于是「音频秒数 ≈ token 数 / 12」—— 据此给出确定性的时长上限。
TTS_FRAMES_PER_SEC = 12.0

# 语速常量只用于估算上限，取偏快一侧；宁可上限宽松，也不要误杀正常语速
_CJK_PER_SEC = 3.7        # 中文实测（本项目的 12 页中文讲稿）
_EN_WORDS_PER_SEC = 2.2   # 英文
_PAUSE_PER_BREAK = 0.35   # 句间 \n\n\n 的静音


def _estimate_seconds(text):
    """估算一段讲稿的朗读时长（秒）。只用于给生成设上限。"""
    cjk = len(re.findall(r"[\u4e00-\u9fff]", text))
    words = len(re.findall(r"[A-Za-z]+", text))
    return (cjk / _CJK_PER_SEC + words / _EN_WORDS_PER_SEC
            + text.count("\n\n\n") * _PAUSE_PER_BREAK)


def _cap_tokens(text):
    """给一段讲稿算一个**宽松但有界**的 token 上限。

    2.5 倍余量 + 固定冗余：正常语速不会撞上；退化生成会被硬截断。
    """
    return int(max(6.0, _estimate_seconds(text) * 2.5) * TTS_FRAMES_PER_SEC) + 180


def inference_qwen3tts(text_prompt, save_path, ref_audio, ref_text, language=None,
                       max_new_tokens=None):
    """零样本音色克隆合成，写入 save_path；**返回实际音频时长（秒）**。

    language 传 ``None`` 走自动语种适配（中英混排推荐）。
    ``max_new_tokens`` 是本项目对付**退化生成**的护栏（见上方常量区注释）：
    给它一个时长上限，最坏也只是这一页在到达上限时被截断，
    而不会让整条流水线卡在十分钟以上。
    """
    import soundfile as sf

    os.makedirs(path.dirname(str(save_path)) or ".", exist_ok=True)
    prompt = get_clone_prompt(ref_audio, ref_text)
    kwargs = {}
    if max_new_tokens:
        kwargs["max_new_tokens"] = int(max_new_tokens)
    wavs, sr = get_tts_model().generate_voice_clone(
        text=text_prompt,
        language=language,
        voice_clone_prompt=prompt,
        **kwargs
    )
    sf.write(str(save_path), wavs[0], sr)
    return len(wavs[0]) / float(sr)


def synth_page_qwen3tts(text_prompt, save_path, ref_audio, ref_text, language=None,
                        page_label=""):
    """合成一页讲稿，带**两级退化护栏**。返回 ``(路径, 时长秒)``。

    ① 整页生成带宽松上限；② 撞上限（= 判为退化）则逐句生成再拼接。
    """
    import numpy as np
    import soundfile as sf

    cap = _cap_tokens(text_prompt)
    dur = inference_qwen3tts(text_prompt, save_path, ref_audio, ref_text,
                             language=language, max_new_tokens=cap)
    if dur < cap / TTS_FRAMES_PER_SEC * 0.95:
        return save_path, dur

    print("[speech_gen] {} 撞到时长上限（实得 {:.1f}s / 上限 {:.1f}s）—— 判为退化生成，"
          "改为逐句合成".format(page_label or "该页", dur, cap / TTS_FRAMES_PER_SEC))

    sentences = [s.strip() for s in text_prompt.split("\n\n\n") if s.strip()]
    if len(sentences) <= 1:
        sentences = [s.strip() for s in
                     re.split(r"(?<=[。！？!?；;])", text_prompt) if s.strip()]
    if len(sentences) <= 1:
        print("[speech_gen] {} 无法再分句，保留被截断的结果".format(page_label or "该页"))
        return save_path, dur

    parts, sr = [], None
    for i, sent in enumerate(sentences):
        tmp = "{}.part{}.wav".format(path.splitext(str(save_path))[0], i)
        inference_qwen3tts(sent, tmp, ref_audio, ref_text, language=language,
                           max_new_tokens=_cap_tokens(sent))
        wav, sr = sf.read(tmp, dtype="float32")
        os.remove(tmp)
        if parts:
            parts.append(np.zeros(int(sr * 0.35), dtype="float32"))
        parts.append(wav)
    merged = np.concatenate(parts)
    sf.write(str(save_path), merged, sr)
    dur = len(merged) / float(sr)
    print("[speech_gen] {} 逐句合成完成：{} 句 -> {:.1f}s".format(
        page_label or "该页", len(sentences), dur))
    return save_path, dur


def inference_f5(text_prompt, save_path, ref_audio, ref_text):
    """[已弃用] 原 F5-TTS 实现（CC-BY-NC，不可商用），需自行安装 f5-tts。"""
    try:
        from f5_tts.api import F5TTS
    except ImportError as exc:
        raise RuntimeError(
            "F5-TTS 已从默认依赖中移除（许可证为 CC-BY-NC，禁止商用）。"
            "如需回退，请自行 pip install f5-tts。原始报错: {}".format(exc)
        )
    f5tts = F5TTS()
    f5tts.infer(ref_file=ref_audio, ref_text=ref_text, gen_text=text_prompt,
                file_wave=save_path, seed=None)


def parse_script(script_text):
    pages = script_text.strip().split("###\n")
    result = []
    for page in pages:
        if not page.strip():
            continue
        lines = page.strip().split("\n")
        page_data = []
        for line in lines:
            if "|" not in line:
                continue
            text, cursor = line.split("|", 1)
            page_data.append([text.strip(), cursor.strip()])
        result.append(page_data)
    return result


def tts_per_slide(model_type, script_path, speech_save_dir, ref_audio, ref_text=None,
                  language=None):
    """逐页合成语音。

    与原版的差异
    ------------
    - ``model_type`` 支持 ``"qwen3tts"``（默认推荐）与 ``"f5"``（旧路径）。
    - **参考文本只转写一次**。原版把 ``transcribe_with_whisperx(ref_audio)``
      写在逐页循环内，等于每页都重新加载 ASR 模型并重新转写同一段音频。
    - 音色 prompt 通过 :func:`get_clone_prompt` 跨页复用。
    """
    with open(script_path, "r", encoding="utf-8") as handle:
        script_with_cursor = "".join(handle.readlines())
    parsed_speech = parse_script(script_with_cursor)

    os.makedirs(speech_save_dir, exist_ok=True)

    # 参考文本只需一次（原来在循环内，是明显的性能 bug）
    if ref_text is None:
        print("[speech_gen] 参考音频未给文本，先用 Qwen3-ASR 转写一次")
        ref_text = transcribe_with_qwen3asr(ref_audio, lang=language)

    # 结构化进度的计数：用「已完成页数」而不是循环下标 ——
    # 上面有「无文本页 skip」的分支，用下标会让 done 虚高。
    _progress_total = len(parsed_speech)
    _progress_done = 0

    for slide_idx in range(len(parsed_speech)):
        speech_with_cursor = parsed_speech[slide_idx]
        subtitle = ""
        for prompt, _cursor_prompt in speech_with_cursor:
            subtitle = prompt if len(subtitle) == 0 else subtitle + "\n\n\n" + prompt

        if not subtitle.strip():
            print("[speech_gen] 第 {} 页无文本，跳过".format(slide_idx))
            continue

        speech_result_path = path.join(speech_save_dir, "{}.wav".format(str(slide_idx)))

        if model_type == "qwen3tts":
            # 走带护栏的入口：退化生成最多被截断，不会让整轮卡住
            synth_page_qwen3tts(subtitle, speech_result_path, ref_audio, ref_text,
                                language=language,
                                page_label="第 {} 页".format(slide_idx))
        elif model_type == "f5":
            inference_f5(subtitle, speech_result_path, ref_audio, ref_text)
        else:
            raise ValueError("未知 model_type: {}（可选 qwen3tts / f5）".format(model_type))

        print("[speech_gen] 第 {} 页合成完成 -> {}".format(slide_idx, speech_result_path))
        _progress_done += 1
        if progress_report:
            progress_report.report("tts", stage=2, done=_progress_done,
                                   total=_progress_total, page=slide_idx)
