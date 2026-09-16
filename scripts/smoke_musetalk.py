#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""MuseTalk 数字人冒烟测试（端到端，走**真实生产入口**）。

与 smoke_longcat.py 的区别
--------------------------
smoke_longcat.py **绕过 subprocess** 直接调模型（为的是量显存峰值）；这个反过来
——**不碰模型代码**，直接调 ``talking_gen.talking_gen_per_slide("musetalk", ...)``。

目标不是「模型能不能跑」，而是**集成代码对不对**：单图 + 音频能否变成
「落在契约路径、带音轨」的 ``merge_video.mp4``。所以它顺带覆盖了四件容易出错的事：

1. 暂存到 ASCII 工作区（MuseTalk 用 ``cv2.imread``，读不了中文路径）
2. 模型权重路径（unet/whisper 显式传，vae/face-parse 靠 cwd 相对的硬编码）
3. 产物取回规则 ``<result_dir>/<version>/<name>.mp4``
4. 搬回**中文路径**下 ``<save_dir>/<idx>/<ref_stem>/`` 的最终位置

用法（在**项目 venv** 里跑；它会自己拉起 MuseTalk 的 venv 子进程）
------------------------------------------------------------------

    .venv/Scripts/python.exe scripts/smoke_musetalk.py

可调环境变量
------------
- ``PAPER2VIDEO_SMOKE_TALKING_DIR``  产物根目录，默认 ``<repo>/result/_smoke_musetalk``
  （刻意放在含中文的仓库路径下，从而把「搬回中文路径」这一步也测到）
- ``PAPER2VIDEO_SMOKE_SECONDS``      只取音频前 N 秒（默认 0 = 整段）
- ``PAPER2VIDEO_SMOKE_REF`` / ``PAPER2VIDEO_SMOKE_AUDIO``  换素材
"""

import json
import os
import shutil
import subprocess
import sys
import time

PROJECT_SRC = os.environ.get(
    "PAPER2VIDEO_SRC", r"E:\Development\26年\paper2video\Paper2Video-main\src")
sys.path.insert(0, PROJECT_SRC)

import pv_env  # noqa: E402
import talking_config as tc  # noqa: E402
import talking_gen as tg  # noqa: E402

ASSETS = os.path.join(PROJECT_SRC, "..", "assets", "demo")

REF_IMG = os.environ.get("PAPER2VIDEO_SMOKE_REF", os.path.join(ASSETS, "zeyu.png"))
REF_AUDIO = os.environ.get("PAPER2VIDEO_SMOKE_AUDIO", os.path.join(ASSETS, "zeyu.wav"))

# 默认放在仓库的 result/ 下 —— 该路径含中文（E:\Development\26年\...），
# 于是「生成完搬回中文路径」这一步也被覆盖。
SMOKE_DIR = os.environ.get(
    "PAPER2VIDEO_SMOKE_TALKING_DIR",
    os.path.join(pv_env.REPO_ROOT, "result", "_smoke_musetalk"))

REQUIRE_SECONDS = float(os.environ.get("PAPER2VIDEO_SMOKE_SECONDS", "0") or 0)


def ffprobe(path):
    """用 ffprobe 读流信息，返回 (视频流 dict, 音频流 dict 或 None)。"""
    cmd = [shutil.which("ffprobe") or "ffprobe", "-v", "error",
           "-show_streams", "-print_format", "json", path]
    raw = subprocess.run(cmd, capture_output=True, text=True).stdout
    streams = json.loads(raw).get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    return video, audio


def prepare_audio(workspace):
    """按需截取音频前 N 秒，避免冒烟测试跑满整个音频。"""
    if REQUIRE_SECONDS <= 0:
        return REF_AUDIO
    out = os.path.join(workspace, "smoke_audio.wav")
    cmd = [shutil.which("ffmpeg") or "ffmpeg", "-y", "-v", "error",
           "-i", REF_AUDIO, "-t", str(REQUIRE_SECONDS), out]
    subprocess.run(cmd, check=True)
    print("[smoke] 音频已截取前 {}s -> {}".format(REQUIRE_SECONDS, out))
    return out


def main():
    profile_name = os.environ.get("PAPER2VIDEO_SMOKE_PROFILE", "musetalk-local")
    profile = tc.resolve_profile(profile_name, backend="musetalk")

    print("=" * 72)
    print("MuseTalk 冒烟测试")
    print("  档位     : {}".format(tc.describe_profile(profile)))
    print("  参考图   : {}".format(REF_IMG))
    print("  音频     : {}".format(REF_AUDIO))
    print("  产物根   : {}  <- 含中文，刻意为之".format(SMOKE_DIR))
    print("=" * 72)

    # staging 工作区（talking_gen 内部对 idx=0 用的是同一个路径）
    workspace = tg._ascii_workspace("smoke", "musetalk")
    gen_workspace = tg._ascii_workspace(0, "musetalk")

    # ── 清理上次残留 ──
    # 残留的 merge_video.mp4 会让 talking_gen 直接跳过，什么都测不到；
    # 残留的 <workspace>/out/<version>/musetalk_out.mp4 也有风险：子进程失败时
    # 可能被当成「产物已存在」。
    # （talking_gen._invoke_musetalk 已经用 mtime 做了新鲜度校验，所以这里只是
    # 让冒烟测试有个干净起点，不是唯一防线。）
    #
    # ⚠️ 但**不能**用 rmtree 清整个 workspace：代理沙箱里 python 的 PYTHONPATH 被
    # 指向 WorkBuddy 的 shim 目录，那里 monkeypatch 了删除操作，单次 >50 个目标就
    # 抛异常并让脚本直接退出（实测踩过：SAFE_DELETE_BULK_CONFIRM_REQUIRED
    # count=256）。而且那个开关是 shim 在 **import 时**读的
    # （sitecustomize.py:35），运行时改环境变量没用 —— 必须在启动解释器之前设置，
    # 脚本自己救不了自己。所以这里只删那一个文件（单文件删除不受守卫限制）。
    stale_out = tc.musetalk_output_path(profile, os.path.join(gen_workspace, "out"))
    if os.path.exists(stale_out):
        os.remove(stale_out)
        print("[smoke] 已删除上次残留的产物 {}".format(stale_out))

    if os.path.isdir(SMOKE_DIR):
        shutil.rmtree(SMOKE_DIR)
    os.makedirs(SMOKE_DIR, exist_ok=True)

    audio = prepare_audio(workspace)

    t0 = time.time()
    results = tg.talking_gen_per_slide(
        "musetalk", [[REF_IMG, audio]], SMOKE_DIR,
        pv_env.gpu_list(), env_path="", profile=profile_name)
    elapsed = time.time() - t0

    print()
    print("-" * 72)
    ok = bool(results) and all(r[1] for r in results)
    if not ok:
        print("[smoke] 失败：{}".format(results))
        # 把子进程的原始日志位置指出来，方便定位
        print("[smoke] 子进程输出在上面（承自 talking_gen 的 stdout）")
        return 1

    target = results[0][2]
    print("[smoke] 耗时 {:.0f}s（含模型加载）".format(elapsed))
    print("[smoke] 产物：{}".format(target))
    print("[smoke] 大小 {:.2f} MB".format(os.path.getsize(target) / 2 ** 20))

    # ① 契约路径复核（merge_talking.py 用的是同一套层级）
    idx = 0
    ref_stem = os.path.splitext(os.path.basename(REF_IMG))[0]
    expect = os.path.join(SMOKE_DIR, str(idx), ref_stem, "merge_video.mp4")
    if os.path.normcase(os.path.abspath(target)) != os.path.normcase(os.path.abspath(expect)):
        print("[smoke] ❌ 产物不在契约路径！预期 {}".format(expect))
        return 1
    print("[smoke] ✓ 契约路径正确")

    # ② 音轨（无音轨时 merge_talking.py 会静默跳过该页）
    video, audio_stream = ffprobe(target)
    if audio_stream is None:
        print("[smoke] ❌ 无音轨 —— merge_talking.py 会跳过该页")
        return 1
    print("[smoke] ✓ 有音轨：{} {}Hz {}ch".format(
        audio_stream.get("codec_name"), audio_stream.get("sample_rate"),
        audio_stream.get("channels")))

    # ③ 视频参数
    if video is None:
        print("[smoke] ❌ 无视频流")
        return 1
    fps_expr = video.get("r_frame_rate", "0/1")
    try:
        num, den = (int(x) for x in fps_expr.split("/"))
        fps = num / den if den else 0
    except ValueError:
        fps = 0
    duration = float(video.get("duration") or 0)
    print("[smoke] ✓ 视频：{} {}x{} {:.2f}fps 时长{:.1f}s".format(
        video.get("codec_name"), video.get("width"), video.get("height"),
        fps, duration))

    wanted_fps = profile.get("fps", 25)
    if abs(fps - wanted_fps) > 0.5:
        print("[smoke] ⚠️ 帧率 {} 与档位的 {} 不符".format(fps, wanted_fps))

    print()
    print("SMOKE_OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
