"""本地 Qwen3 语音合成、参考音频转写与字词对齐。

模型惰性加载，音色 prompt 跨页复用；合成结果保留时长护栏与课程分块缓存。
"""

import os
import re
import shutil
from os import path
from pathlib import Path

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


def parse_script(script_text):
    from script_validation import parse_script as parse
    return parse(script_text)


def _reference_key(audio_sha, language):
    import course
    return course.digest({'audio': audio_sha, 'language': language, 'asr': ASR_REPO, 'v': 1})


def _cached_reference_text(directory, audio_sha, language):
    import course
    saved = course.read_json(path.join(directory, 'reference.json'), {})
    if isinstance(saved, dict) and saved.get('key') == _reference_key(audio_sha, language):
        text = saved.get('text')
        if isinstance(text, str) and text.strip():
            return text


def _local_voice_key(audio_sha, ref_text, language, model_type):
    import course
    # Keep the existing identity so already completed recordings remain reusable.
    return course.digest({'audio': audio_sha, 'text': ref_text, 'language': language,
                          'model': model_type, 'repo': TTS_REPO, 'v': 1})


def _valid_local_audio(filename, saved, key):
    import course
    import soundfile as sf
    try:
        if not isinstance(saved, dict) or saved.get('key') != key:
            return False
        with open(filename, 'rb') as handle:
            if saved.get('wav_sha') != course.digest(handle.read()):
                return False
        info = sf.info(filename)
        return info.frames > 0 and info.samplerate > 0
    except (OSError, ValueError, RuntimeError):
        return False


def _archive_local_audio(filename, saved):
    import course
    key = saved.get('key') if isinstance(saved, dict) else None
    if not isinstance(key, str) or not re.fullmatch(r'[0-9a-f]{64}', key) or not _valid_local_audio(filename, saved, key):
        return
    version = Path(filename).parent / '.versions' / (key + '.wav')
    version.parent.mkdir(parents=True, exist_ok=True)
    if not _valid_local_audio(version, saved, key):
        shutil.copy2(filename, version)
    course.write_json(str(version) + '.json', saved)


def _restore_local_audio(filename, key):
    import course
    version = Path(filename).parent / '.versions' / (key + '.wav')
    saved = course.read_json(str(version) + '.json', {})
    if _valid_local_audio(version, saved, key):
        shutil.copy2(version, filename)
        return saved


def local_audio_status(script_path, directory, ref_audio, ref_text=None, language=None, model_type='qwen3tts'):
    """Read-only resume inspection, using the same identities as synthesis."""
    import course
    with open(script_path, encoding='utf-8') as handle:
        pages = parse_script(handle.read())
    manifest = course.read_json(path.join(directory, 'manifest.json'), {})
    manifest = manifest if isinstance(manifest, dict) else {}
    with open(ref_audio, 'rb') as handle:
        audio_sha = course.digest(handle.read())
    if ref_text is None:
        ref_text = _cached_reference_text(directory, audio_sha, language)
    voice_key = _local_voice_key(audio_sha, ref_text, language, model_type)
    reusable = 0
    for index, lines in enumerate(pages):
        text = '\n\n\n'.join(prompt for prompt, _ in lines)
        key = course.digest({'voice': voice_key, 'text': text})
        version = Path(directory) / '.versions' / (key + '.wav')
        valid = _valid_local_audio(path.join(directory, f'{index}.wav'), manifest.get(str(index), {}), key)
        if not valid:
            valid = _valid_local_audio(version, course.read_json(str(version) + '.json', {}), key)
        if text.strip() and ref_text is not None and valid:
            reusable += 1
    return {'total_pages': len(pages), 'reusable_pages': reusable, 'needs_synthesis': len(pages) - reusable}


def tts_per_slide(model_type, script_path, speech_save_dir, ref_audio, ref_text=None,
                  language=None, resume_audio=True, model_snapshot=None, force_pages=(), generation_id=None):
    """逐页合成语音。

    与原版的差异
    ------------
    - ``model_type`` 当前仅支持 ``"qwen3tts"``。
    - 参考文本缺失时只转写一次，不在逐页循环中重复识别。
    - 音色 prompt 通过 :func:`get_clone_prompt` 跨页复用。
    """
    if model_snapshot and model_snapshot.get('model_config_origin') == 'v2' and model_snapshot['model_config']['stages']['tts']['mode'] == 'api':
        from bailian_speech import tts_pages
        with open(script_path, encoding='utf-8') as handle:
            parsed = parse_script(handle.read())
        tts_pages(parsed, speech_save_dir, model_snapshot, resume_audio, force_pages=force_pages, generation_id=generation_id)
        return
    if model_type != "qwen3tts":
        raise ValueError("不支持的配音模型: {}（当前仅支持 qwen3tts）".format(model_type))

    with open(script_path, "r", encoding="utf-8") as handle:
        script_with_cursor = "".join(handle.readlines())
    parsed_speech = parse_script(script_with_cursor)
    if force_pages and (not isinstance(force_pages, (list, tuple)) or any(type(index) is not int or not 0 <= index < len(parsed_speech) for index in force_pages)
                        or not isinstance(generation_id, str) or not re.fullmatch(r'[a-f0-9]{32}', generation_id)):
        raise ValueError('配音修复需要有效页索引和恢复标识')

    os.makedirs(speech_save_dir, exist_ok=True)

    import course
    with open(ref_audio, 'rb') as handle:
        audio_sha = course.digest(handle.read())
    # 参考文本只需一次（原来在循环内，是明显的性能 bug）
    if ref_text is None:
        ref_text = _cached_reference_text(speech_save_dir, audio_sha, language) if resume_audio else None
        if ref_text is None:
            print("[speech_gen] 参考音频未给文本，先用 Qwen3-ASR 转写一次")
            ref_text = transcribe_with_qwen3asr(ref_audio, lang=language)
            if not ref_text.strip():
                raise ValueError('参考音频转写为空，请补充参考文本')
            course.write_json(path.join(speech_save_dir, 'reference.json'),
                              {'key': _reference_key(audio_sha, language), 'text': ref_text})

    # 结构化进度的计数：用「已完成页数」而不是循环下标 ——
    # 上面有「无文本页 skip」的分支，用下标会让 done 虚高。
    _progress_total = len(parsed_speech)
    _progress_done = 0

    manifest_path = path.join(speech_save_dir, "manifest.json")
    manifest = course.read_json(manifest_path, {}) if resume_audio else {}
    manifest = manifest if isinstance(manifest, dict) else {}
    voice_key = _local_voice_key(audio_sha, ref_text, language, model_type) if resume_audio else None
    if resume_audio:
        status = local_audio_status(script_path, speech_save_dir, ref_audio, ref_text, language, model_type)
        print('[speech_gen] 配音续跑检查：{reusable_pages}/{total_pages} 页有效缓存，本次需合成 {needs_synthesis} 页'.format(**status), flush=True)

    for slide_idx in range(len(parsed_speech)):
        speech_with_cursor = parsed_speech[slide_idx]
        subtitle = ""
        for prompt, _cursor_prompt in speech_with_cursor:
            subtitle = prompt if len(subtitle) == 0 else subtitle + "\n\n\n" + prompt

        if not subtitle.strip():
            print("[speech_gen] 第 {} 页无文本，跳过".format(slide_idx))
            continue

        speech_result_path = path.join(speech_save_dir, "{}.wav".format(str(slide_idx)))

        if resume_audio:
            import soundfile as sf
            import numpy as np
            key = course.digest({"voice": voice_key, "text": subtitle})
            cached = manifest.get(str(slide_idx), {})
            forced = slide_idx in force_pages and cached.get('repair_generation') != generation_id
            if not _valid_local_audio(speech_result_path, cached, key):
                _archive_local_audio(speech_result_path, cached)
                restored = None if forced else _restore_local_audio(speech_result_path, key)
                if restored:
                    cached = manifest[str(slide_idx)] = restored
                    course.write_json(manifest_path, manifest)
            if not forced and _valid_local_audio(speech_result_path, cached, key):
                print("[course] 第 {} 页音频复用".format(slide_idx + 1), flush=True)
            else:
                # 长页主动分段，失败保留已完成段；避免一次生成数分钟语音。
                sentences = [s for s in re.split(r"(?<=[。！？!?；;])|\n{2,}", subtitle) if s.strip()]
                chunks = []
                for sentence in sentences:
                    while len(sentence) > 400:
                        boundary = sentence.rfind(" ", 200, 400)
                        boundary = boundary if boundary > 0 else 400
                        chunks.append(sentence[:boundary].strip())
                        sentence = sentence[boundary:]
                    if sentence.strip():
                        if chunks and len(chunks[-1]) + len(sentence) < 400:
                            chunks[-1] += " " + sentence.strip()
                        else:
                            chunks.append(sentence.strip())
                parts, sr = [], None
                part_dir = path.join(speech_save_dir, ".parts", str(slide_idx))
                os.makedirs(part_dir, exist_ok=True)
                for j, chunk in enumerate(chunks):
                    part_key = course.digest({"voice": voice_key, "chunk": chunk})
                    if slide_idx in force_pages:
                        part_key = course.digest({'part': part_key, 'repair_generation': generation_id})
                    # 按内容保存分段，避免改稿后覆盖旧讲稿的恢复依据。
                    part = path.join(part_dir, part_key + ".wav")
                    stamp = part + ".json"
                    saved = course.read_json(stamp, {})
                    legacy = path.join(part_dir, str(j) + '.wav')
                    legacy_saved = course.read_json(legacy + '.json', {})
                    if slide_idx not in force_pages and not _valid_local_audio(part, saved, part_key) and _valid_local_audio(legacy, legacy_saved, part_key):
                        shutil.copy2(legacy, part)
                        saved = legacy_saved
                        course.write_json(stamp, saved)
                    if not _valid_local_audio(part, saved, part_key):
                        synth_page_qwen3tts(chunk, part, ref_audio, ref_text, language=language, page_label="{}-{}".format(slide_idx + 1, j + 1))
                        course.write_json(stamp, {"key": part_key, "wav_sha": course.digest(Path(part).read_bytes())})
                    wav, part_sr = sf.read(part, dtype="float32")
                    if sr is not None and sr != part_sr:
                        raise RuntimeError("音频分段采样率不一致")
                    sr = part_sr
                    if parts:
                        parts.append(np.zeros(int(sr * 0.35), dtype="float32"))
                    parts.append(wav)
                merged = np.concatenate(parts)
                temporary = speech_result_path + '.tmp.wav'
                sf.write(temporary, merged, sr)
                os.replace(temporary, speech_result_path)
                manifest[str(slide_idx)] = {"key": key, "text_sha": course.digest(subtitle),
                    "wav_sha": course.digest(Path(speech_result_path).read_bytes()), "seconds": len(merged) / sr,
                    **({'repair_generation': generation_id} if slide_idx in force_pages else {})}
                course.write_json(manifest_path, manifest)
            _progress_done += 1
            if progress_report:
                progress_report.report("tts", stage=2, done=_progress_done, total=_progress_total, page=slide_idx)
            continue

        synth_page_qwen3tts(subtitle, speech_result_path, ref_audio, ref_text,
                            language=language,
                            page_label="第 {} 页".format(slide_idx))

        print("[speech_gen] 第 {} 页合成完成 -> {}".format(slide_idx, speech_result_path))
        _progress_done += 1
        if progress_report:
            progress_report.report("tts", stage=2, done=_progress_done,
                                   total=_progress_total, page=slide_idx)
