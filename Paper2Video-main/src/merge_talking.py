"""把数字人视频叠到幻灯片上并串联成片 —— `1_merage.bash` 的 Python 等价实现。

为什么要重写
------------
上游 `pipeline.py` 调的是 `./1_merage.bash`，它在本机有三处不可行：
1. Windows 无法直接执行 bash 脚本；
2. 脚本把 `list.txt` 写在**当前工作目录**、且内部用相对路径，cwd 一变就崩；
3. 「取时长再 loop 幻灯片」依赖 shell 命令替换，跨平台难以稳定复现。

本实现与原脚本的对应关系（刻意保持一致，便于对照排查）
--------------------------------------------------------
原脚本                              | 本实现
------------------------------------|------------------------------------------
`$VIDEO_DIR/$((i-1))/$ref_img/...`  | 同一层级（音频从 0 起、图片从 1 起）
`ffprobe` 检查有无音轨、无则跳过      | 同样检查并跳过（避免生成无声片段）
`[1:v]scale=W:H` + `overlay=W-w-10:10` | 同样的缩放与右上角叠加
`-t "$duration"` 循环幻灯片           | 同样的按数字人时长循环
`ffmpeg -f concat -c copy`           | 一致（分片编码参数相同，可无损拼接）

产物依旧是 `page_%03d.mp4` 分片 + 一个合并后的总片。
"""

import os
import subprocess
from os import path

from merge_video import _as_posix, _run, probe_duration


def _has_audio_stream(video_path):
    """用 ffprobe 判断视频是否含音轨。

    原 bash 里这一步是必须的：没有音轨的分片被 concat 进来会让整片音画错位，
    所以宁可直接跳过该页。
    """
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a",
         "-show_entries", "stream=codec_type",
         "-of", "default=noprint_wrappers=1:nokey=1", str(video_path)],
        capture_output=True, text=True,
    )
    return bool(result.stdout.strip())


def merge_slides_and_talking(slide_dir, talking_dir, output_dir, video_path,
                             num_video, ref_img, width, height, offset=10):
    """把数字人视频叠加到对应幻灯片右上角，再串联为 ``video_path``。

    参数
    ----
    slide_dir   : 幻灯片目录（`1.png` 起）
    talking_dir : 数字人产物根目录，层级为 `<idx>/<ref_img>/merge_video.mp4`
    output_dir  : 分片输出目录
    video_path  : 最终合并视频路径
    num_video   : 页数
    ref_img     : 参考图主文件名（不含扩展名），用于拼层级
    width/height: 数字人缩放到的大小
    offset      : 距右边缘与上边缘的像素间距（原脚本是 10）
    """
    os.makedirs(output_dir, exist_ok=True)
    parent = path.dirname(path.abspath(video_path))
    if parent:
        os.makedirs(parent, exist_ok=True)

    segments = []
    for index in range(1, int(num_video) + 1):
        slide_path = path.join(slide_dir, "{}.png".format(index))
        talking_path = path.join(talking_dir, str(index - 1), ref_img, "merge_video.mp4")

        if not path.isfile(slide_path):
            print("  [跳过] 第 {} 页：找不到幻灯片 {}".format(index, slide_path))
            continue
        if not path.isfile(talking_path):
            print("  [跳过] 第 {} 页：找不到数字人视频 {}".format(index, talking_path))
            continue
        if not _has_audio_stream(talking_path):
            print("  [跳过] 第 {} 页：数字人视频无音轨".format(index))
            continue

        duration = probe_duration(talking_path)
        out_path = path.join(output_dir, "page_{:03d}.mp4".format(index))

        # 幻灯片按数字人时长循环，右上角叠加缩放过的人像。
        # 全程走绝对路径（转正斜杠），避免 ffmpeg 在 Windows 上解析相对路径出错。
        filter_complex = (
            "[1:v]scale={w}:{h}[avatar];"
            "[0:v][avatar]overlay=W-w-{off}:{off}[outv]".format(
                w=int(width), h=int(height), off=int(offset))
        )
        _run([
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-loop", "1", "-t", "{:.3f}".format(duration), "-i", _as_posix(slide_path),
            "-i", _as_posix(talking_path),
            "-filter_complex", filter_complex,
            "-map", "[outv]", "-map", "1:a",
            "-c:v", "libx264", "-c:a", "aac", "-preset", "fast", "-crf", "23",
            "-pix_fmt", "yuv420p", "-shortest",
            _as_posix(out_path),
        ])
        print("  [合成] 第 {} 页  时长 {:.2f}s".format(index, duration))
        segments.append(out_path)

    if not segments:
        raise RuntimeError(
            "没有任何可用分片：请先确认数字人视频已生成且带音轨"
            "（目录 {}）".format(talking_dir))

    # 分片参数完全一致，可以无损拼接。
    #
    # ⚠️ list 里的路径**必须是绝对路径**。ffmpeg 的 concat 解复用器把 list 里的
    # 相对路径按「**list 文件所在目录**」解析，而不是按 cwd —— 与本文件原先写的
    # cwd 相对路径（`../../result/xxx/merage/page_001.mp4`）一叠加，就变成了
    #
    #     <前导>/merage/../../result/xxx/merage/page_001.mp4
    #
    # ffmpeg 报 `Impossible to open ...`，且退出码是 4294967294（即 -2），
    # 看起来像被信号杀掉，很不好读。
    #
    # 实测触发条件：`--result_dir` 传**相对**路径时必崩；传绝对路径时侥幸能过 ——
    # 因为那时 `output_dir` 本身就是绝对的。这正是模块 docstring 里说的上游那个
    # 「list 写在哪、内容相对谁」的耦合，本实现原本只挪了 list 的位置，没解决
    # 内容这一半。改成 abspath 后两种传法都稳。
    list_path = path.join(output_dir, "concat_list.txt")
    with open(list_path, "w", encoding="utf-8") as handle:
        for seg in segments:
            handle.write("file '{}'\n".format(_as_posix(path.abspath(seg))))

    _run([
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-f", "concat", "-safe", "0", "-i", _as_posix(list_path),
        "-c", "copy", _as_posix(video_path),
    ])
    print("数字人与幻灯片合成完成（{} 段）-> {}".format(len(segments), video_path))
    return video_path
