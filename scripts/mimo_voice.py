"""音色生成（MiMo-V2.5-TTS）—— **只用来生成一段参考音频**，逐页合成仍走本地 Qwen3-TTS。

为什么是这个架构（而不是用 MiMo 逐页合成讲稿）：见 `docs/音色方案.md`。一句话：
音色一次定下 → 全片必然同一个人声；逐页合成仍本地 → 免费期结束/断网/无 key
都不影响出片；字幕对齐链路一行不用改。

三种来源
--------
- ``preset``  预置音色（文档给的 8 个精品音色，含中英男女）—— 最稳，推荐
- ``design``  文本描述设计音色（``mimo-v2.5-tts-voicedesign``）—— 有随机性，
              所以界面上必须支持"试听 + 重掷 + 保存"
- ``upload``  使用者自己上传的音频（由 web 层直接落盘，不经过本模块）

产物与缓存
----------
``jobs/_voices/<key>.wav`` + 同名 ``.txt``（参考文本）+ ``.json``（元数据）；
``key = sha1(kind|voice|prompt|lang)`` ⇒ **同一个音色只生成一次**，
之后所有任务共用同一份 wav（音色稳定，也不再消耗配额）。

两条硬约束（都是踩过坑得来的）
------------------------------
1. **参考文本必须由我们给定**。文档里 ``optimize_text_preview=True`` 可以让模型
   自己润色待合成文本、从而省掉 ``assistant`` 消息 —— 但那样我们就不知道它念了
   什么，``ref_text`` 失控，只能回头让 ASR 转写；而本项目实测 Qwen3-ASR 对参考
   音频会转出**错的文本**，错误的 ref_text 会直接拉低零样本克隆的保真度。
   所以固定由我们传入 ``assistant`` 文本：它既是朗读内容，也就是 ``ref_text``。
2. **绝不打印 key**。所有回显走 ``mask()``。

CLI
---
    python scripts/mimo_voice.py --list
    python scripts/mimo_voice.py --kind preset --voice 冰糖 --lang zh
    python scripts/mimo_voice.py --kind design --prompt "…" --lang zh
"""
import argparse
import base64
import hashlib
import io
import json
import os
import sys
import time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC_DIR = os.path.join(REPO_ROOT, "Paper2Video-main", "src")
CACHE_DIR = os.path.join(REPO_ROOT, "jobs", "_voices")

# 把 .env 载入 os.environ —— 必须在读取 MIMO_API_KEY **之前**完成。
# 踩过的坑：`available()` 直接读 os.environ，而 .env 是 `pv_env` 在 import 时
# 才解析的；结果 key 明明配好了，界面却说"未配置"（CLI `--list` 也一样）。
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)
try:
    import pv_env  # noqa: F401  载入仓库根的 .env（不覆盖已存在的环境变量）
except Exception:                                  # pragma: no cover
    pv_env = None

MODEL_PRESET = "mimo-v2.5-tts"
MODEL_DESIGN = "mimo-v2.5-tts-voicedesign"
DEFAULT_BASE = "https://api.xiaomimimo.com/v1"

# ── 参考文本（我们自己给定；它既是让 MiMo 朗读的内容，也是克隆用的 ref_text）──
#
# 为什么不随便找一句话：文档明确说"合成文本要贴合音色"——给温柔治愈系女声配一段
# 晚安独白效果最好。反过来，拿与用途无关的极端文本去造音色，得到的音色会偏离
# "论文讲解"这个使用场景。所以这两段刻意写成**中性学术讲解开场白**，
# 长度落在 12~16 秒（实测），正好在 Qwen3-TTS 克隆的理想区间内。
REF_TEXTS = {
    "zh": ("大家好，欢迎观看本次论文讲解。接下来我会用几分钟时间，介绍这项研究的"
           "研究背景、主要方法与实验结果，希望能帮助你快速理解它的核心贡献。"),
    "en": ("Hello, and welcome to this paper presentation. Over the next few minutes I will "
           "walk you through the research background, the main method and the experimental "
           "results, so that you can quickly grasp the core contribution of this work."),
}

# 风格指令（放 user 消息）。场景与用途一致，音色更贴合讲解。
STYLES = {
    "zh": "用清晰平稳的学术讲解语气，语速适中，像在讲台上介绍一项研究。",
    "en": ("Clear, steady academic presentation tone, moderate pace, "
           "like introducing a study on stage."),
}

# 预置音色（来自官方文档的"预置音色列表"）。
# ⚠️ 刻意不包含 `mimo_default`：它"因部署集群而异"（中国集群默认冰糖、其他集群
#    默认 Mia），同一份配置在不同部署上会出不同音色 —— 显式指定才可复现。
PRESET_VOICES = [
    {"id": "冰糖", "label": "冰糖 · 中文女声", "lang": "zh", "gender": "female"},
    {"id": "茉莉", "label": "茉莉 · 中文女声", "lang": "zh", "gender": "female"},
    {"id": "苏打", "label": "苏打 · 中文男声", "lang": "zh", "gender": "male"},
    {"id": "白桦", "label": "白桦 · 中文男声", "lang": "zh", "gender": "male"},
    {"id": "Mia", "label": "Mia · 英文女声", "lang": "en", "gender": "female"},
    {"id": "Chloe", "label": "Chloe · 英文女声", "lang": "en", "gender": "female"},
    {"id": "Milo", "label": "Milo · 英文男声", "lang": "en", "gender": "male"},
    {"id": "Dean", "label": "Dean · 英文男声", "lang": "en", "gender": "male"},
]

# 参考音频的可用区间。上限不是硬要求（只影响克隆编码耗时），下限是硬要求：
# 太短会让克隆不稳定 —— 宁可明确拒绝，也不要产出一个人声音色乱漂的成片。
MIN_SECONDS = 3.0
MAX_SECONDS = 30.0


class VoiceError(Exception):
    """带 kind 的音色错误，便于界面给出可操作的提示，而不是一句"失败了"。"""

    def __init__(self, kind, message, detail=""):
        super().__init__(message)
        self.kind = kind            # no_key / auth / rate_limit / network / bad_response / too_short
        self.message = message
        self.detail = detail


def mask(value):
    """任何要显示/落日志的 key 都先过这里 —— 永远不打印完整 key。"""
    if not value:
        return ""
    if len(value) <= 10:
        return "*" * len(value)
    return "{}...{}".format(value[:4], value[-4:])


def api_key():
    """从环境变量取 key（`pv_env` 会把仓库根的 .env 载入 os.environ）。"""
    key = os.environ.get("MIMO_API_KEY") or ""
    return key.strip()


def available():
    """音色生成功能是否可用（没配 key 时界面上要**禁用**并说明原因）。"""
    return bool(api_key())


def _client():
    key = api_key()
    if not key:
        raise VoiceError("no_key",
                         "未配置 MIMO_API_KEY —— 无法生成音色（可使用内置音色或上传自己的音频）",
                         "在仓库根的 .env 里加一行 MIMO_API_KEY=...")
    sys.path.insert(0, SRC_DIR)
    try:
        import pv_env  # noqa: F401  确认 .env 已载入
    except Exception:
        pass
    from openai import OpenAI
    base = os.environ.get("MIMO_API_BASE") or DEFAULT_BASE
    return OpenAI(api_key=api_key(), base_url=base, timeout=180)


def cache_key(kind, voice, prompt, lang):
    raw = "|".join([kind or "", voice or "", prompt or "", lang or ""])
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _paths(key):
    return (os.path.join(CACHE_DIR, key + ".wav"),
            os.path.join(CACHE_DIR, key + ".txt"),
            os.path.join(CACHE_DIR, key + ".json"))


def cached(key):
    """缓存命中则返回元数据 dict（含 ref_audio / ref_text），否则 None。"""
    wav, txt, meta = _paths(key)
    if not (os.path.isfile(wav) and os.path.isfile(txt) and os.path.isfile(meta)):
        return None
    try:
        data = json.load(io.open(meta, encoding="utf-8"))
    except Exception:
        return None
    data["ref_audio"] = wav
    data["ref_text"] = io.open(txt, encoding="utf-8").read().strip()
    data["cached"] = True
    return data


def list_cached():
    """已缓存的音色（设计出来的 / 上传的）—— 界面要能把它们列出来复用。

    顺带把 wav 的实际路径与参考文本也带上：上传类音色在界面上"选它"时，
    需要的是 path + ref_text，而不是重新生成一遍。
    """
    out = []
    if not os.path.isdir(CACHE_DIR):
        return out
    for name in sorted(os.listdir(CACHE_DIR)):
        if not name.endswith(".json"):
            continue
        key = name[:-5]
        try:
            data = json.load(io.open(os.path.join(CACHE_DIR, name), encoding="utf-8"))
        except Exception:
            continue
        wav = os.path.join(CACHE_DIR, key + ".wav")
        txt = os.path.join(CACHE_DIR, key + ".txt")
        if not os.path.isfile(wav):
            continue                                  # 元数据在、音频没了 → 不可用
        data["key"] = key
        data["ref_audio"] = wav
        data["ref_text"] = (io.open(txt, encoding="utf-8").read().strip()
                            if os.path.isfile(txt) else None)
        out.append(data)
    out.sort(key=lambda d: d.get("created_at", ""), reverse=True)
    return out


def _read_duration(path):
    try:
        import soundfile as sf
        return float(sf.info(path).duration)
    except Exception:
        return None


def generate(kind, voice=None, prompt=None, lang="zh", force=False):
    """生成（或取缓存）一段参考音频。返回元数据 dict。

    ``kind="preset"`` 需要 voice；``kind="design"`` 需要 prompt。
    `force=True` 时忽略缓存（界面上"重新生成"用）。
    """
    if lang not in REF_TEXTS:
        raise VoiceError("bad_request", "不支持的语言：{}".format(lang))
    key = cache_key(kind, voice, prompt, lang)
    if not force:
        hit = cached(key)
        if hit:
            return hit

    if kind == "preset":
        model, audio = MODEL_PRESET, {"format": "wav", "voice": voice}
        if not voice:
            raise VoiceError("bad_request", "预置音色必须指定音色名")
    elif kind == "design":
        model, audio = MODEL_DESIGN, {"format": "wav"}
        if not prompt or not prompt.strip():
            raise VoiceError("bad_request", "设计音色必须给出音色描述")
    else:
        raise VoiceError("bad_request", "未知的音色来源：{}".format(kind))

    client = _client()
    messages = [{"role": "user", "content": prompt.strip() if kind == "design" else STYLES[lang]},
                # ⚠️ 必须由我们给定（见模块 docstring 的硬约束 1）
                {"role": "assistant", "content": REF_TEXTS[lang]}]
    try:
        resp = client.chat.completions.create(model=model, messages=messages, audio=audio)
    except Exception as exc:
        raise _classify(exc)

    message = resp.choices[0].message
    payload = getattr(message, "audio", None)
    if payload is None:
        raise VoiceError("bad_response", "接口没有返回音频",
                         "模型只说了一句话：%r" % (getattr(message, "content", "") or "")[:200])
    b64 = payload["data"] if isinstance(payload, dict) else getattr(payload, "data", None)
    if not b64:
        raise VoiceError("bad_response", "返回的音频为空")
    raw = base64.b64decode(b64)

    wav, txt, meta = _paths(key)
    os.makedirs(CACHE_DIR, exist_ok=True)
    with io.open(wav, "wb") as handle:
        handle.write(raw)
    with io.open(txt, "w", encoding="utf-8", newline="") as handle:
        handle.write(REF_TEXTS[lang])
    duration = _read_duration(wav)
    if duration is not None and duration < MIN_SECONDS:
        os.remove(wav)
        raise VoiceError("too_short", "生成的参考音频只有 %.1f 秒（至少 %.0f 秒）"
                         % (duration, MIN_SECONDS))
    data = {
        "key": key, "kind": kind, "voice": voice, "prompt": prompt, "lang": lang,
        "model": model, "style": messages[0]["content"],
        "ref_text": REF_TEXTS[lang], "ref_audio": wav,
        "duration_s": round(duration, 2) if duration else None,
        "bytes": len(raw), "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "cached": False,
    }
    with io.open(meta, "w", encoding="utf-8", newline="") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
    return data


def _classify(exc):
    """把 SDK 异常翻译成可操作的提示。

    为什么要分类：余额/限流/网络/鉴权在界面上要做不同的事（换 key / 稍后重试 /
    检查网络），只报一句"请求失败"会让使用者无从下手 —— 这一点和
    `/api/key/verify` 里对 DeepSeek 的处理是同一个道理。
    """
    text = "{}".format(exc)
    low = text.lower()
    if any(k in low for k in ("401", "unauthorized", "invalid api key", "authentication")):
        kind, msg = "auth", "MiMo 鉴权失败 —— key 不正确或已失效"
    elif any(k in low for k in ("429", "rate limit", "too many")):
        kind, msg = "rate_limit", "MiMo 限流 —— 稍后重试（不影响已缓存的音色）"
    elif any(k in low for k in ("insufficient", "quota", "balance", "欠费")):
        kind, msg = "quota", "MiMo 配额不足"
    elif any(k in low for k in ("timeout", "connection", "resolve", "network", "ssl")):
        kind, msg = "network", "连不上 MiMo —— 检查网络/代理"
    else:
        kind, msg = "unknown", "MiMo 调用失败"
    return VoiceError(kind, msg, text[:300])


def describe_voice(spec, script_lang):
    """把界面传来的音色选择翻译成一句人能读的说明（写进任务日志）。"""
    kind = (spec or {}).get("kind") or "default"
    if kind == "preset":
        return "预置音色 {}".format(spec.get("voice") or "?")
    if kind == "design":
        return "设计音色（{}）".format((spec.get("prompt") or "")[:30])
    if kind == "upload":
        return "上传的参考音频 {}".format(os.path.basename(spec.get("path") or "?"))
    return "内置默认音色（{}）".format(script_lang)


def resolve(spec, script_lang):
    """把音色选择解析成 (ref_audio, ref_text, 说明)。

    **任何失败都不抛异常**，而是回落成 (None, None, 说明) —— 让流水线用语言档位
    的内置参考音频继续跑。理由：音色是"锦上添花"，不该变成新的故障点
    （限时免费、网络抖动、别人改了 key 都可能让它失败，这些都不该拦住出片）。
    """
    spec = spec or {}
    kind = spec.get("kind") or "default"
    if kind == "default":
        return None, None, describe_voice(spec, script_lang)
    if kind == "upload":
        path = spec.get("path")
        if path and os.path.isfile(path):
            text = (spec.get("ref_text") or "").strip() or None
            return path, text, describe_voice(spec, script_lang)
        return None, None, "上传的参考音频已不可用 -> 回落内置默认音色"
    try:
        data = generate(kind, voice=spec.get("voice"), prompt=spec.get("prompt"),
                        lang=script_lang, force=bool(spec.get("force")))
        origin = "缓存" if data.get("cached") else "新生成"
        return data["ref_audio"], data["ref_text"], "%s（%s）" % (
            describe_voice(spec, script_lang), origin)
    except VoiceError as exc:
        return None, None, "音色生成失败（%s：%s）-> 回落内置默认音色" % (exc.kind, exc.message)
    except Exception as exc:                       # 兜底：绝不让音色拖垮任务
        return None, None, "音色生成异常（%s）-> 回落内置默认音色" % type(exc).__name__


def voices_for(script_lang):
    """按讲稿语言给候选音色，**每项自带可直接提交的 `spec`**。

    为什么把 spec 组装放在后端：前端只需要"把选中的 spec 原样塞进 config"，
    不用知道预置/设计/上传三种来源的字段差异 —— 少一处前后端各拼一遍的地方，
    也就少一处会漂的地方（这个项目已经在"产物路径"上踩过同一个坑）。
    跨语言克隆音色会漂，所以设计类音色按语言过滤；上传类不过滤（是使用者自己
    放的，他知道自己在做什么），但界面上会标出它的语言未知。
    """
    presets = [{"label": v["label"], "gender": v["gender"], "lang": v["lang"],
                "kind": "preset", "spec": {"kind": "preset", "voice": v["id"]}}
               for v in PRESET_VOICES if v["lang"] == script_lang]
    for item in presets:
        saved = cached(cache_key("preset", item["spec"]["voice"], None, script_lang))
        if saved:
            item["sample_url"] = "/api/voices/{}/sample".format(saved["key"])
            item["duration_s"] = saved.get("duration_s")
            item["ref_text"] = saved.get("ref_text")
    custom = []
    for item in list_cached():
        kind = item.get("kind")
        if kind == "design" and item.get("lang") != script_lang:
            continue
        if kind in (None, "preset"):
            continue
        if kind == "upload":
            spec = {"kind": "upload", "path": item["ref_audio"], "ref_text": item.get("ref_text")}
            label = "我的音频 · {}".format(item.get("filename") or item["key"])
        else:
            spec = {"kind": "design", "prompt": item.get("prompt"), "voice": item.get("voice")}
            label = "我的音色 · {}".format((item.get("prompt") or "?")[:18])
        custom.append({"label": label, "kind": kind, "lang": item.get("lang"),
                        "duration_s": item.get("duration_s"), "ref_text": item.get("ref_text"), "spec": spec,
                       "sample_url": "/api/voices/{}/sample".format(item["key"])})
    return {"presets": presets, "custom": custom}


def main():
    parser = argparse.ArgumentParser(description="生成音色参考音频（MiMo-V2.5-TTS）")
    parser.add_argument("--list", action="store_true", help="列出预置音色与已缓存的音色")
    parser.add_argument("--kind", choices=["preset", "design"], default="preset")
    parser.add_argument("--voice", help="预置音色名（如 冰糖 / 苏打 / Chloe）")
    parser.add_argument("--prompt", help="音色描述（design 模式）")
    parser.add_argument("--lang", default="zh", choices=sorted(REF_TEXTS))
    parser.add_argument("--force", action="store_true", help="忽略缓存，重新生成")
    args = parser.parse_args()

    if args.list:
        print("可用：MIMO_API_KEY %s" % ("已配置" if available() else "**未配置**"))
        print("预置音色：")
        for v in PRESET_VOICES:
            print("   %-6s %-16s %s" % (v["id"], v["label"], v["lang"]))
        cache = list_cached()
        print("已缓存音色：%d 个" % len(cache))
        for c in cache:
            print("   %-16s %-8s %-5s %.1fs  %s" % (
                c.get("key"), c.get("kind"), c.get("lang"),
                c.get("duration_s") or 0, (c.get("prompt") or c.get("voice") or "")[:40]))
        return 0

    try:
        data = generate(args.kind, voice=args.voice, prompt=args.prompt,
                        lang=args.lang, force=args.force)
    except VoiceError as exc:
        print("✘ [%s] %s" % (exc.kind, exc.message))
        if exc.detail:
            print("   %s" % exc.detail)
        return 1
    print("%s 音色已就绪（%s）" % ("缓存命中" if data.get("cached") else "新生成",
                                   "key=" + data["key"]))
    print("   参考音频 %s（%.1f 秒）" % (data["ref_audio"], data.get("duration_s") or 0))
    print("   参考文本 %s" % data["ref_text"][:40] + "…")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
