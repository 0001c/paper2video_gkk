"""Paper2Video 模块级冒烟测试。

为什么需要它
------------
整条 pipeline 很长（LaTeX 编译 -> LLM 出稿 -> TTS -> 转写对齐 -> 光标定位 ->
合片），任何一环没装好都要等到跑完整条链才暴露。这个脚本把每个**替换后的模型**
单独拉出来验证，出问题时能立刻定位到具体环节。

覆盖的环节
----------
    env     torch / CUDA / sm_120 / 路径
    llm     DeepSeek 文本 + **图像**调用（含 reasoning 模型的两个坑）
    latex   tectonic 编译仓库自带 slides.tex -> PDF -> PNG（不需要 LLM）
    asr     Qwen3-ASR 转写参考音频，结果存盘供 tts 复用
    tts     Qwen3-TTS 零样本音色克隆合成
    align   Qwen3-ForcedAligner 词级时间戳
    cursor  GUI-Owl-1.5-4B grounding（用 latex 步骤产出的真实幻灯片）

显存提示
--------
16 GB 单卡上不要同时持有多个模型。本脚本在每个环节结束后主动释放，
顺序也按「后一个不需要前一个」来排。

用法
----
    ./.venv/Scripts/python.exe scripts/smoke_test.py            # 全部（按依赖顺序）
    ./.venv/Scripts/python.exe scripts/smoke_test.py --only llm latex
    ./.venv/Scripts/python.exe scripts/smoke_test.py --list
"""

import argparse
import base64
import io
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "Paper2Video-main" / "src"
sys.path.insert(0, str(SRC))

import pv_env  # noqa: E402  （必须在 sys.path 之后导入）

# 产物一律落在纯英文目录：项目路径含中文，ffmpeg 的 ass 滤镜无法处理
OUT = Path(os.environ.get("PAPER2VIDEO_SMOKE_DIR", r"E:\ai-models\smoke"))
DEMO = ROOT / "Paper2Video-main" / "assets" / "demo"
LATEX_PROJ = DEMO / "latex_proj"

# 顺序即执行顺序：后一步可能依赖前一步的产物
ALL_TESTS = ["env", "llm", "latex", "asr", "tts", "align", "cursor"]

_state = {}


def head(title):
    print("\n" + "=" * 74)
    print("  {}".format(title))
    print("=" * 74)


def ok(msg):
    print("  [OK]   {}".format(msg))


def warn(msg):
    print("  [WARN] {}".format(msg))


def fail(msg):
    print("  [FAIL] {}".format(msg))


# ──────────────────────────────────────────────────────────────
# env
# ──────────────────────────────────────────────────────────────

def test_env():
    head("env · 运行环境")
    pv_env.print_banner()
    import torch
    if not torch.cuda.is_available():
        fail("CUDA 不可用 —— 后续所有 GPU 环节都会失败")
        return False
    cap = torch.cuda.get_device_capability()
    if cap[0] < 12:
        fail("compute capability sm_{}{}，cu128 未生效".format(cap[0], cap[1]))
        return False
    ok("sm_{}{} 可用".format(cap[0], cap[1]))
    free, total = torch.cuda.mem_get_info()
    print("         显存 空闲 {:.1f} GB / 共 {:.1f} GB".format(free / 1e9, total / 1e9))
    for mod in ("transformers", "qwen_tts", "qwen_asr", "cv2", "fitz", "soundfile", "camel"):
        try:
            __import__(mod)
            ok("import {}".format(mod))
        except Exception as exc:  # noqa: BLE001
            fail("import {} -> {}".format(mod, type(exc).__name__))
    tectonic = Path(pv_env.TOOLS_BIN) / "tectonic.exe"
    print("         tectonic: {}".format(tectonic if tectonic.is_file() else "缺失"))
    return True


# ──────────────────────────────────────────────────────────────
# llm
# ──────────────────────────────────────────────────────────────

def _chat(payload, timeout=300):
    key = pv_env.require_api_key("OPENAI_API_KEY")
    base = os.environ.get("OPENAI_API_BASE", "https://api.deepseek.com/v1").rstrip("/")
    req = urllib.request.Request(
        base + "/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def test_llm():
    head("llm · DeepSeek 文本与图像调用")
    model = os.environ.get("PAPER2VIDEO_LLM_MODEL", "deepseek-flash")

    # ── 文本 ──
    t0 = time.time()
    try:
        data = _chat({
            "model": model,
            "messages": [{"role": "user", "content": "只回答两个字：正常"}],
            "max_tokens": 512,
        })
    except urllib.error.HTTPError as exc:
        fail("HTTP {} {}".format(exc.code, exc.read().decode("utf-8", "replace")[:200]))
        return False
    except Exception as exc:  # noqa: BLE001
        fail("{}: {}".format(type(exc).__name__, str(exc)[:200]))
        return False

    msg = data["choices"][0]["message"]
    usage = data.get("usage", {})
    content = (msg.get("content") or "").strip()
    reasoning = (msg.get("reasoning_content") or "")
    print("         模型 {:<18} 耗时 {:.1f}s".format(model, time.time() - t0))
    print("         content = {!r}".format(content[:80]))
    print("         reasoning_content 长度 = {}".format(len(reasoning)))
    print("         usage = {}".format(usage))
    if not content:
        fail("content 为空 —— 这是 reasoning 模型最典型的坑："
             "思考过程占满了 max_tokens，正文根本没输出")
        return False
    ok("文本调用正常")

    # ── 图像 ──
    image_path = DEMO / "zeyu.png"
    if not image_path.is_file():
        warn("缺少 {}，跳过图像测试".format(image_path))
        return True
    b64 = base64.b64encode(image_path.read_bytes()).decode()
    t0 = time.time()
    try:
        data = _chat({
            "model": model,
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": "这张图里有一个人物吗？只回答 有 或 无。"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64," + b64}},
            ]}],
            "max_tokens": 1024,
        })
    except Exception as exc:  # noqa: BLE001
        fail("图像调用失败 {}: {}".format(type(exc).__name__, str(exc)[:200]))
        return False
    msg = data["choices"][0]["message"]
    content = (msg.get("content") or "").strip()
    print("         耗时 {:.1f}s  content = {!r}".format(time.time() - t0, content[:80]))
    if not content:
        fail("图像调用 content 为空")
        return False
    ok("图像输入可用（无需再依赖 Gemini）")
    return True


# ──────────────────────────────────────────────────────────────
# latex
# ──────────────────────────────────────────────────────────────

def test_latex():
    head("latex · tectonic 编译自带 demo（不经过 LLM）")
    tex = LATEX_PROJ / "slides.tex"
    if not tex.is_file():
        fail("找不到 {}".format(tex))
        return False

    tectonic = Path(pv_env.TOOLS_BIN) / "tectonic.exe"
    if not tectonic.is_file():
        fail("找不到 tectonic：{}（先跑 scripts/setup_tools.py）".format(tectonic))
        return False

    out_dir = OUT / "latex"
    out_dir.mkdir(parents=True, exist_ok=True)
    # 直接在原始工程目录里编译：slides.tex 用 \input{section/...} 引用相对路径，
    # 复制出来会找不到章节文件。产物写到 out_dir。
    t0 = time.time()
    proc = subprocess.run(
        [str(tectonic), "-X", "compile", str(tex),
         "--outdir", str(out_dir), "--keep-logs"],
        capture_output=True, text=True, cwd=str(LATEX_PROJ), timeout=900,
    )
    dt = time.time() - t0
    print("         退出码 {} 耗时 {:.1f}s".format(proc.returncode, dt))
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "")[-1200:]
        fail("tectonic 编译失败：\n{}".format(tail))
        return False

    pdf = out_dir / "slides.pdf"
    if not pdf.is_file():
        fail("退出码为 0 但没生成 slides.pdf")
        return False
    ok("slides.pdf 生成 {:.2f} MB".format(pdf.stat().st_size / 1e6))

    # PDF -> PNG（验证 pdf_render 去掉 poppler 依赖后仍可用）
    try:
        from pdf_render import render_pdf_to_files
        pages = render_pdf_to_files(str(pdf), str(out_dir), dpi=200)
    except Exception as exc:  # noqa: BLE001
        fail("pdf_render 失败 {}: {}".format(type(exc).__name__, str(exc)[:200]))
        return False
    ok("渲染出 {} 张幻灯片".format(len(pages)))
    _state["slides"] = pages
    return True


# ──────────────────────────────────────────────────────────────
# asr
# ──────────────────────────────────────────────────────────────

def test_asr():
    head("asr · Qwen3-ASR 转写")
    ref_audio = DEMO / "zeyu.wav"
    if not ref_audio.is_file():
        fail("找不到参考音频 {}".format(ref_audio))
        return False

    t0 = time.time()
    try:
        from speech_gen import transcribe_with_qwen3asr, release_models
        text = transcribe_with_qwen3asr(str(ref_audio), lang=None)
    except Exception as exc:  # noqa: BLE001
        fail("{}: {}".format(type(exc).__name__, str(exc)[:400]))
        return False

    print("         耗时 {:.1f}s".format(time.time() - t0))
    print("         转写结果：{!r}".format(text[:200]))
    if not text.strip():
        fail("转写结果为空")
        return False

    ref_txt = OUT / "ref_transcript.txt"
    ref_txt.parent.mkdir(parents=True, exist_ok=True)
    ref_txt.write_text(text, encoding="utf-8")
    ok("转写成功，已存 {}".format(ref_txt))
    _state["ref_text"] = text

    release_models(("asr",))
    return True


# ──────────────────────────────────────────────────────────────
# tts
# ──────────────────────────────────────────────────────────────

def test_tts():
    head("tts · Qwen3-TTS 零样本音色克隆")
    ref_audio = DEMO / "zeyu.wav"
    if not ref_audio.is_file():
        fail("找不到参考音频 {}".format(ref_audio))
        return False

    ref_text = _state.get("ref_text")
    if not ref_text:
        cached = OUT / "ref_transcript.txt"
        if cached.is_file():
            ref_text = cached.read_text(encoding="utf-8")
    if not ref_text:
        warn("没有参考文本，改由模型自行编码音色（x_vector_only）")

    sentence = ("Hello, this is a smoke test of the Paper2Video pipeline. "
                "We replaced F5 TTS with Qwen3 TTS.")
    out_wav = OUT / "tts" / "sample.wav"

    t0 = time.time()
    try:
        from speech_gen import inference_qwen3tts, release_models
        inference_qwen3tts(sentence, str(out_wav), str(ref_audio), ref_text, language="English")
    except Exception as exc:  # noqa: BLE001
        fail("{}: {}".format(type(exc).__name__, str(exc)[:400]))
        return False

    if not out_wav.is_file() or out_wav.stat().st_size < 1024:
        fail("输出文件缺失或过小：{}".format(out_wav))
        return False

    import soundfile as sf
    info = sf.info(str(out_wav))
    print("         耗时 {:.1f}s".format(time.time() - t0))
    print("         输出 {:.2f} MB  {:.1f} kHz  {:.2f} 秒".format(
        out_wav.stat().st_size / 1e6, info.samplerate / 1000, info.duration))
    if info.duration < 1.0:
        fail("音频时长过短（{:.2f}s），合成可能异常".format(info.duration))
        return False
    ok("语音合成成功")
    _state["tts_wav"] = str(out_wav)
    _state["tts_text"] = sentence

    release_models(("tts",))
    return True


# ──────────────────────────────────────────────────────────────
# align
# ──────────────────────────────────────────────────────────────

def test_align():
    head("align · Qwen3-ForcedAligner 词级时间戳")
    wav = _state.get("tts_wav") or str(OUT / "tts" / "sample.wav")
    text = _state.get("tts_text")
    if not os.path.isfile(wav):
        fail("找不到待对齐音频 {}（先跑 tts）".format(wav))
        return False
    if not text:
        fail("缺少待对齐文本（由 tts 步骤提供）")
        return False

    t0 = time.time()
    try:
        from speech_gen import align_with_qwen3, release_models
        aligned = align_with_qwen3(wav, text, lang="English")
    except Exception as exc:  # noqa: BLE001
        fail("{}: {}".format(type(exc).__name__, str(exc)[:400]))
        return False

    words = aligned["segments"][0]["words"]
    print("         耗时 {:.1f}s  得到 {} 个词条".format(time.time() - t0, len(words)))
    if not words:
        fail("对齐结果为空 —— cursor_gen.timesteps() 会拿不到时间轴")
        return False
    for w in words[:6]:
        print("           {:<16} {:.3f}s -> {:.3f}s".format(w["word"][:16], w["start"], w["end"]))
    if len(words) > 6:
        print("           ...")
        print("           {:<16} {:.3f}s -> {:.3f}s".format(
            words[-1]["word"][:16], words[-1]["start"], words[-1]["end"]))

    # 关键结构校验：下游按 aligned["segments"][i]["words"][j]["end"] 取值
    assert isinstance(aligned, dict) and "segments" in aligned
    assert all({"word", "start", "end"} <= set(w) for w in words)
    ok("whisperx 兼容结构校验通过")

    (OUT / "align.json").write_text(
        json.dumps(aligned, ensure_ascii=False, indent=2), encoding="utf-8")
    release_models(("aligner",))
    return True


# ──────────────────────────────────────────────────────────────
# cursor
# ──────────────────────────────────────────────────────────────

def test_cursor():
    head("cursor · GUI-Owl-1.5-4B grounding")

    slides = _state.get("slides") or []
    if not slides:
        slides = sorted(str(p) for p in (OUT / "latex").glob("*.png"))
    if not slides:
        fail("没有可用幻灯片图片（先跑 latex）")
        return False
    image_path = slides[0]
    print("         使用幻灯片：{}".format(os.path.basename(image_path)))

    import cv2
    img = cv2.imread(image_path)
    h, w = img.shape[:2]
    print("         图片尺寸 {}x{}".format(w, h))

    instructions = [
        "Click on the title of this slide.",
        "Click on the figure in this slide.",
    ]

    results = []
    t0 = time.time()
    try:
        from cursor_gen import infer_cursor, release_gui_model
        # 第一次调用会加载模型，单独计时
        first_t0 = time.time()
        for idx, instruction in enumerate(instructions):
            t = time.time()
            point, raw = infer_cursor(instruction, image_path)
            if idx == 0:
                print("         模型首次加载 + 推理 {:.1f}s".format(time.time() - first_t0))
            print("         指令: {!r}".format(instruction))
            print("           -> 像素坐标 {}   耗时 {:.1f}s".format(point, time.time() - t))
            print("           原始输出: {!r}".format(raw[:160]))
            results.append((instruction, point, raw))
    except Exception as exc:  # noqa: BLE001
        fail("{}: {}".format(type(exc).__name__, str(exc)[:400]))
        return False
    finally:
        try:
            from cursor_gen import release_gui_model
            release_gui_model()
        except Exception:  # noqa: BLE001
            pass

    print("         总耗时 {:.1f}s".format(time.time() - t0))

    good = [r for r in results if r[1] is not None]
    if not good:
        fail("全部指令都解析不出坐标 —— 检查 build_grounding_prompt 是否照搬了官方工具协议")
        return False
    ok("{}/{} 条指令解析出坐标".format(len(good), len(results)))

    # 落盘一份带标记的图，方便肉眼核对落点是否合理
    try:
        from cursor_gen import draw_red_dots_on_image
        marked = OUT / "cursor" / "marked.png"
        marked.parent.mkdir(parents=True, exist_ok=True)
        draw_red_dots_on_image(image_path, good[0][1], radius=14, out_path=str(marked))
        ok("落点标注图 -> {}".format(marked))
    except Exception as exc:  # noqa: BLE001
        warn("标注图生成失败（不影响结论）: {}".format(str(exc)[:120]))

    (OUT / "cursor.json").write_text(
        json.dumps([{"instruction": i, "point": p, "raw": r[:400]} for i, p, r in results],
                   ensure_ascii=False, indent=2), encoding="utf-8")
    return True


# ──────────────────────────────────────────────────────────────
# 入口
# ──────────────────────────────────────────────────────────────

TESTS = {
    "env": test_env,
    "llm": test_llm,
    "latex": test_latex,
    "asr": test_asr,
    "tts": test_tts,
    "align": test_align,
    "cursor": test_cursor,
}


def main():
    parser = argparse.ArgumentParser(description="Paper2Video 模块级冒烟测试")
    parser.add_argument("--only", nargs="+", choices=ALL_TESTS,
                        help="只跑指定环节（按给定顺序）")
    parser.add_argument("--list", action="store_true", help="列出全部环节")
    args = parser.parse_args()

    if args.list:
        for name in ALL_TESTS:
            print("  {:>7}  {}".format(name, TESTS[name].__doc__ or ""))
        return 0

    targets = args.only or ALL_TESTS
    OUT.mkdir(parents=True, exist_ok=True)
    print("产物目录：{}".format(OUT))
    print("执行环节：{}".format(", ".join(targets)))

    results = {}
    for name in targets:
        t0 = time.time()
        try:
            results[name] = bool(TESTS[name]())
        except Exception as exc:  # noqa: BLE001
            fail("{} 抛异常 {}: {}".format(name, type(exc).__name__, str(exc)[:300]))
            results[name] = False
        print("  —— {} 用时 {:.1f}s".format(name, time.time() - t0))

    head("汇总")
    for name in targets:
        print("  {:<8} {}".format(name, "通过" if results[name] else "未通过"))
    passed = sum(1 for v in results.values() if v)
    print("\n  {}/{} 通过".format(passed, len(targets)))
    return 0 if passed == len(targets) else 1


if __name__ == "__main__":
    sys.exit(main())
