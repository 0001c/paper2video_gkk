#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""验证上游 MuseTalk 的「偶数尺寸」补丁是否真的生效。

为什么要单独一个脚本：这条命令对**引号**极其敏感
（f-string 用双引号定界 + `os.system` 走 cmd.exe 只用双引号），
手写一条等价命令很容易在多层转义里测出假结果 —— 已经栽过两次：
一次注入未转义引号导致 SyntaxError，一次测试脚本自己双重转义导致误判。

所以这里**不重写命令**：直接从补丁后的源码里取出那一行、`eval` 求值，
再交给 `os.system` 执行。没有任何转义猜测的余地。

做法：造若干张**奇数尺寸**帧（如 215x221）→ 跑上游那两步
（帧序列→temp.mp4、合音频→page_000.mp4）→ 用 ffprobe 核验产物。

用法：
    python scripts/verify_musetalk_patch.py                    # 用 .env 里的 MuseTalk 路径
    python scripts/verify_musetalk_patch.py --musetalk-root E:/ai-models/MuseTalk
    python scripts/verify_musetalk_patch.py --frames 8 --keep  # 保留中间文件便于排查

退出码 0 = 补丁生效（产物含视频流且宽高为偶数）；1 = 未生效或产物不合格。
"""
import argparse
import io
import os
import shutil
import subprocess
import sys
import tempfile
import wave

ODD_WIDTH, ODD_HEIGHT = 215, 221


def extract_expression(script_path, keyword):
    """取出 ``keyword = f"..."`` 那一行等号右边的表达式。"""
    with io.open(script_path, encoding="utf-8") as handle:
        for line in handle:
            stripped = line.strip()
            if stripped.startswith(keyword) and "=" in stripped:
                return stripped.split("=", 1)[1].strip()
    raise AssertionError("在 {} 里没找到 {} 这一行".format(script_path, keyword))


def make_odd_frames(frame_dir, count):
    from PIL import Image
    os.makedirs(frame_dir, exist_ok=True)
    for i in range(count):
        Image.new("RGB", (ODD_WIDTH, ODD_HEIGHT), (180, 160, 140)).save(
            os.path.join(frame_dir, "%08d.png" % i))


def make_silent_wav(path, seconds=1.0, rate=16000):
    with wave.open(path, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(b"\x00\x00" * int(rate * seconds))


def probe(path):
    """返回 (是否含视频流, 宽, 高, 是否含音频流)。"""
    fmt = "csv=p=0"
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,width,height",
         "-of", fmt, path], capture_output=True, text=True)
    rows = [line for line in r.stdout.splitlines() if line.strip()]
    has_video = any(row.startswith("video") for row in rows)
    has_audio = any(row.startswith("audio") for row in rows)
    width = height = None
    for row in rows:
        parts = row.split(",")
        if parts[0] == "video":
            # csv 输出形如 video,1920,1080
            try:
                _kind, width, height = parts[0], int(parts[1]), int(parts[2])
            except (IndexError, ValueError):
                pass
    return has_video, width, height, has_audio


def main():
    parser = argparse.ArgumentParser(description="验证 MuseTalk 偶数尺寸补丁")
    parser.add_argument("--musetalk-root", default=None,
                        help="MuseTalk 仓库根目录；留空则尝试从 .env / talking_config 解析")
    parser.add_argument("--frames", type=int, default=6, help="造几张奇数帧（默认 6）")
    parser.add_argument("--keep", action="store_true", help="保留临时目录")
    args = parser.parse_args()

    root = args.musetalk_root
    if not root:
        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        sys.path.insert(0, os.path.join(repo_root, "Paper2Video-main", "src"))
        try:
            import talking_config
            root = talking_config.resolve_paths(backend="musetalk").repo_dir
        except Exception as exc:
            print("   ✘ 无法解析 MuseTalk 路径（用 --musetalk-root 显式指定）：%s" % exc)
            return 1

    script = os.path.join(root, "scripts", "realtime_inference.py")
    print("=== 目标 ===")
    print("   MuseTalk 根目录 : %s" % root)
    print("   批量入口脚本    : %s" % script)
    if not os.path.isfile(script):
        print("   ✘ 脚本不存在")
        return 1

    text = io.open(script, encoding="utf-8").read()
    patched = "crop=trunc(iw/2)" in text
    print("   补丁状态        : %s" % ("已打（含 crop=trunc）" if patched else "**未打**"))

    work = tempfile.mkdtemp(prefix="p2v_musetalk_patch_")
    try:
        frames_dir = os.path.join(work, "tmp")
        out_dir = os.path.join(work, "vid_output")
        os.makedirs(out_dir, exist_ok=True)
        make_odd_frames(frames_dir, args.frames)
        wav = os.path.join(work, "drive.wav")
        make_silent_wav(wav)

        class FakeSelf(object):
            avatar_path = work
            video_out_path = out_dir

        ns = {
            "fps": 25,
            "self": FakeSelf(),
            "audio_path": wav,
            "out_vid_name": "page_000",
            "output_vid": os.path.join(out_dir, "page_000.mp4"),
        }

        print()
        print("=== ① 帧序列 -> temp.mp4（用源码里的原始命令）===")
        cmd1 = eval(extract_expression(script, "cmd_img2video"), dict(ns))
        print("   %s" % cmd1)
        rc1 = os.system(cmd1)
        temp_mp4 = os.path.join(work, "temp.mp4")
        size1 = os.path.getsize(temp_mp4) if os.path.exists(temp_mp4) else -1
        print("   退出码 %s | temp.mp4 = %d 字节" % (rc1, size1))

        print()
        print("=== ② 合音频 -> page_000.mp4 ===")
        cmd2 = eval(extract_expression(script, "cmd_combine_audio"), dict(ns))
        rc2 = os.system(cmd2)
        out = ns["output_vid"]
        size2 = os.path.getsize(out) if os.path.exists(out) else -1
        print("   退出码 %s | page_000.mp4 = %d 字节" % (rc2, size2))

        print()
        print("=== ③ 产物核验 ===")
        if not os.path.exists(out):
            print("   ✘ 没生成 -> 补丁未生效（这正是 %dx%d 奇数帧的故障表现）"
                  % (ODD_WIDTH, ODD_HEIGHT))
            return 1
        has_video, width, height, has_audio = probe(out)
        print("   视频流 %s | 音频流 %s | 尺寸 %sx%s" % (
            has_video, has_audio, width, height))
        even = (width % 2 == 0) and (height % 2 == 0) if width and height else False
        ok = has_video and has_audio and even
        print()
        print("   => %s" % ("✔ 通过（补丁生效）" if ok
                          else "✘ 不合格（应同时有视频流+音频流，且宽高为偶数）"))
        return 0 if ok else 1
    finally:
        if args.keep:
            print()
            print("   中间文件保留在: %s" % work)
        else:
            shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
