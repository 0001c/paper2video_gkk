"""幻灯片 + 配音合片。

为什么用 Python 重写
--------------------
原实现是 ``1_merage_light.bash``，在 Windows 上完全不可用：
1. 它是 bash 脚本，Windows 无法直接执行（``subprocess.run(["./1_merage_light.bash"])``
   在 cmd/PowerShell 下会直接失败）；
2. 它把 ``list.txt`` 写在**当前工作目录**而不是输出目录，且内部是相对路径，
   一旦 cwd 变化，ffmpeg 的 concat 就找不到分片；
3. 它依赖 ``awk``、``seq``、``printf`` 等 Unix 工具。

这里用 Python 直调 ffmpeg，三个平台通用，且全程使用绝对路径。

功能与原脚本一致：每页幻灯片按对应音频时长静止展示，随后拼接成完整视频。
"""

import os
import shutil
import subprocess
from os import path

AUDIO_EXTS = ("wav", "mp3", "m4a", "aac", "flac", "ogg", "opus")


def _run(cmd, **kwargs):
    """执行外部命令；失败时把 stderr 一并抛出，便于定位。"""
    result = subprocess.run(cmd, capture_output=True, text=True, **kwargs)
    if result.returncode != 0:
        raise RuntimeError(
            "命令失败（exit {}）:\n  {}\n--- stderr ---\n{}".format(
                result.returncode, " ".join(str(c) for c in cmd), result.stderr[-2000:]))
    return result


def probe_duration(audio_path):
    """读取音频时长（秒）。ffprobe 不可用时回退到 soundfile。"""
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(audio_path)],
            capture_output=True, text=True,
        )
        if result.returncode == 0 and result.stdout.strip():
            return float(result.stdout.strip())
    except FileNotFoundError:
        pass

    try:
        import soundfile as sf
        info = sf.info(str(audio_path))
        return float(info.frames) / float(info.samplerate)
    except Exception as exc:
        raise RuntimeError("无法读取音频时长: {} ({})".format(audio_path, exc))


def _as_posix(p):
    """ffmpeg 在 Windows 上对反斜杠敏感，统一转正斜杠。"""
    return str(p).replace("\\", "/")


def merge_slides_and_audio(slide_dir, audio_dir, output_dir, num_video,
                           video_path, ref_img=""):
    """把 num_video 页幻灯片与同名音频合成为 ``video_path``。

    音频文件名按 ``{页码-1}.wav`` 规则匹配（与 TTS 输出保持一致）。
    """
    os.makedirs(output_dir, exist_ok=True)
    parent = path.dirname(path.abspath(video_path))
    if parent:
        os.makedirs(parent, exist_ok=True)

    segments = []
    for index in range(1, int(num_video) + 1):
        slide_path = path.join(slide_dir, "{}.png".format(index))
        if not path.isfile(slide_path):
            print("  [跳过] 第 {} 页：找不到幻灯片 {}".format(index, slide_path))
            continue

        audio_path = None
        for ext in AUDIO_EXTS:
            candidate = path.join(audio_dir, "{}.{}".format(index - 1, ext))
            if path.isfile(candidate):
                audio_path = candidate
                break
        if audio_path is None:
            print("  [跳过] 第 {} 页：{} 下无对应音频".format(index, audio_dir))
            continue

        try:
            duration = probe_duration(audio_path)
        except RuntimeError as exc:
            print("  [跳过] 第 {} 页：{}".format(index, exc))
            continue

        out_path = path.join(output_dir, "page_{:03d}.mp4".format(index))
        print("  [合成] 第 {} 页  音频 {}  {:.2f}s".format(
            index, path.basename(audio_path), duration))

        _run([
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-loop", "1", "-t", "{:.3f}".format(duration), "-i", _as_posix(slide_path),
            "-i", _as_posix(audio_path),
            "-map", "0:v", "-map", "1:a",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-r", "30",
            "-preset", "fast", "-crf", "23",
            "-c:a", "aac", "-b:a", "192k", "-ar", "44100", "-ac", "2",
            "-shortest", _as_posix(out_path),
        ])
        segments.append(out_path)

    if not segments:
        print("  [警告] 没有生成任何片段，跳过拼接")
        return None

    # concat demuxer 的清单文件放在输出目录，且用绝对路径，避免 cwd 依赖
    list_path = path.join(output_dir, "concat_list.txt")
    with open(list_path, "w", encoding="utf-8") as handle:
        for seg in segments:
            handle.write("file '{}'\n".format(_as_posix(path.abspath(seg))))

    print("  [拼接] {} 个片段 -> {}".format(len(segments), video_path))
    _run([
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-f", "concat", "-safe", "0", "-i", _as_posix(path.abspath(list_path)),
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-r", "30",
        "-preset", "fast", "-crf", "23",
        "-c:a", "aac", "-b:a", "192k", "-ar", "44100", "-ac", "2",
        _as_posix(path.abspath(video_path)),
    ])
    print("  [完成] {}".format(video_path))
    return video_path


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="幻灯片 + 配音合片（Python 版）")
    parser.add_argument("slide_dir")
    parser.add_argument("audio_dir")
    parser.add_argument("output_dir")
    parser.add_argument("num_video", type=int)
    parser.add_argument("video_path")
    parser.add_argument("ref_img", nargs="?", default="")
    args = parser.parse_args()

    merge_slides_and_audio(args.slide_dir, args.audio_dir, args.output_dir,
                           args.num_video, args.video_path, args.ref_img)
