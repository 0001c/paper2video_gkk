"""字幕渲染（ffmpeg + ASS 硬字幕）。

为什么要重写
------------
原实现有三处在本机不可行：
1. ``import whisper`` + ``from moviepy.editor import ...``：moviepy 1.0.3 在
   Python 3.13 + numpy 2.x 下会因废弃 API 抛错（``moviepy.editor`` 在 2.x 已被移除）；
   而且为了烧个字幕再装一份 Whisper 权重（约 150 MB）并不划算。
2. 字体写死为 Linux 路径 ``/usr/share/fonts/truetype/liberation/...``，Windows 上必然回退默认字体。
3. ``create_subtitle_image`` 的异常分支里 ``{e}`` 是未定义变量，第一次字体加载失败就会
   抛 ``NameError`` 掩盖真实原因。

新实现
------
时间轴直接复用流水线已经算好的 ``cursor.json``（其中每条都带起止时间与原文），
因此不需要二次转写。字幕用 ASS 格式描述、由 ffmpeg 的 ``ass`` 滤镜烧录：
- ASS 里显式声明 ``PlayResX/Y`` 为视频真实分辨率，于是 ``FontSize`` 与像素字号 1:1 对应；
- ``BorderStyle=3`` 提供半透明底色块，贴近原实现的白字黑底效果；
- 字幕文件写在纯英文临时目录，规避 ffmpeg 在 Windows 上对非 ASCII 路径的解析问题。
"""

import os
import json
import subprocess
import shutil
import tempfile
import unicodedata

import pv_env
import subtitle_style

# libass 通过字体名向系统要字体，这里给跨平台候选
DEFAULT_FONT = "Microsoft YaHei" if os.name == "nt" else "DejaVu Sans"


def _probe_resolution(video_path):
    """读取视频宽高。"""
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-of", "json", str(video_path)],
        capture_output=True, text=True,
    )
    info = json.loads(result.stdout)
    stream = info["streams"][0]
    return int(stream["width"]), int(stream["height"])


def _ass_timestamp(seconds):
    seconds = max(0.0, float(seconds))
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = seconds % 60
    return "{}:{:02d}:{:05.2f}".format(hours, minutes, secs)


def _escape_ass_text(text):
    """转义 ASS 保留字符；换行在 ASS 里是 \\N。"""
    text = str(text).replace("\\", "\\\\").replace("{", "\\{").replace("}", "\\}")
    return text.replace("\r\n", "\\N").replace("\n", "\\N")


def _load_segments(cursor_json_path):
    """从 cursor.json 读取字幕段落。

    优先用 ``raw_text``（未经清洗的原文），其次 ``text``。
    后者是 ``clean_text`` 的产物（全小写、去标点），用于显示不美观，仅在缺 raw_text 时兜底。
    """
    with open(cursor_json_path, "r", encoding="utf-8") as handle:
        data = json.load(handle)

    segments = []
    for item in data:
        if "start" not in item or "end" not in item:
            continue
        text = (item.get("raw_text") or item.get("text") or "").strip()
        if not text:
            continue
        segments.append({"start": float(item["start"]), "end": float(item["end"]), "text": text})
    return segments


def _wrap_text(text, width, font_size):
    capacity = max(1, width / font_size * 0.85)
    lines = []
    for paragraph in str(text).replace('\r\n', '\n').replace('\r', '\n').split('\n'):
        line, used = '', 0.0
        for char in paragraph:
            units = 0 if unicodedata.combining(char) else 1
            if line and used + units > capacity:
                split = line.rfind(' ')
                if split > len(line) // 2:
                    lines.append(line[:split])
                    line = line[split + 1:]
                    used = sum(not unicodedata.combining(item) for item in line)
                else:
                    lines.append(line)
                    line, used = '', 0.0
            line += char
            used += units
        lines.append(line)
    return '\n'.join(lines)


def _ass_color(color, opacity=1):
    return '&H{:02X}{}{}{}'.format(round((1 - opacity) * 255), color[5:7], color[3:5], color[1:3])


def _write_ass(segments, ass_path, width, height, font_size, font_name, style=None):
    """生成 ASS 字幕文件。"""
    style = subtitle_style.normalize(style)
    margin_v = max(10, round(height * style['margin_bottom'] / 100))
    margin_h = max(10, round(width * style['margin_horizontal'] / 100))
    outline = max(1, int(font_size // 16))      # 描边宽度
    background = _ass_color(style['background_color'], style['background_opacity'])
    border_style = 3 if style['background_opacity'] > 0 else 1
    outline_color = background if border_style == 3 else '&H00000000'

    header = """[Script Info]
ScriptType: v4.00+
PlayResX: {w}
PlayResY: {h}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{font},{size},{primary},&H000000FF,{outline_color},{background},0,0,0,0,100,100,0,0,{border_style},{outline},0,2,{mh},{mh},{mv},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
""".format(w=width, h=height, font=font_name, size=int(font_size),
           outline=outline, mv=margin_v, mh=margin_h, primary=_ass_color(style['text_color']),
           outline_color=outline_color, background=background, border_style=border_style)

    lines = [header]
    for seg in segments:
        event_size = int(font_size)
        wrapped = _wrap_text(seg['text'], width - 2 * margin_h, event_size)
        while (wrapped.count('\n') + 1) * event_size * 1.4 > height - 2 * margin_v and event_size > 8:
            event_size -= 1
            wrapped = _wrap_text(seg['text'], width - 2 * margin_h, event_size)
        if (wrapped.count('\n') + 1) * event_size * 1.4 > height - 2 * margin_v:
            raise ValueError('字幕段落过长，无法在画面内完整显示，请先拆分讲稿段落')
        override = r'{\fs' + str(event_size) + '}' if event_size != int(font_size) else ''
        lines.append("Dialogue: 0,{},{},Default,,0,0,0,,{}".format(
            _ass_timestamp(seg["start"]), _ass_timestamp(seg["end"]),
            override + _escape_ass_text(wrapped)))

    with open(ass_path, "w", encoding="utf-8-sig") as handle:
        handle.write("\n".join(lines) + "\n")


def add_subtitles(video_path, output_path, font_size=None,
                  cursor_json_path=None, font_name=None, style=None):
    """把字幕烧录进视频。

    参数
    ----
    cursor_json_path : 流水线产出的 ``cursor.json``，用于取时间轴与文本。
        不传时会尝试与视频同目录的 ``cursor.json``。
    font_name : libass 字体名，默认 Windows 用微软雅黑。
    """
    configured = style is not None
    style = subtitle_style.normalize(style)
    if not style['enabled']:
        shutil.copyfile(video_path, output_path)
        return output_path
    if cursor_json_path is None:
        candidate = os.path.join(os.path.dirname(os.path.abspath(video_path)), "cursor.json")
        if os.path.isfile(candidate):
            cursor_json_path = candidate
    if not cursor_json_path or not os.path.isfile(cursor_json_path):
        raise FileNotFoundError(
            "缺少 cursor.json，无法确定字幕时间轴。"
            "请先执行 stage 2 生成字幕时间轴，或显式传入 cursor_json_path。")

    segments = _load_segments(cursor_json_path)
    if not segments:
        print("[subtitle_render] cursor.json 中没有可用的字幕段落，跳过")
        shutil.copyfile(video_path, output_path)
        return output_path

    width, height = _probe_resolution(video_path)
    font_size = max(1, round(style['font_size'] * height / 1080)) if configured or font_size is None else font_size
    font_name = style['font_name'] or font_name or DEFAULT_FONT
    print("[subtitle_render] 视频 {}x{}，字幕 {} 条，字号 {}".format(
        width, height, len(segments), font_size))

    # ASS 必须落在纯英文目录：ffmpeg 的 ass 滤镜在 Windows 上无法解析中文路径
    os.makedirs(pv_env.TMP_DIR, exist_ok=True)
    descriptor, ass_path = tempfile.mkstemp(prefix='p2v_subtitle_', suffix='.ass', dir=pv_env.TMP_DIR)
    os.close(descriptor)

    # 以 cwd + 纯文件名引用，彻底绕开路径转义问题（冒号、反斜杠在滤镜串里都要转义）
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(os.path.abspath(video_path)),
        "-vf", "ass={}".format(os.path.basename(ass_path)),
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "fast", "-crf", "23",
        "-c:a", "copy",
        str(os.path.abspath(output_path)),
    ]
    try:
        _write_ass(segments, ass_path, width, height, font_size, font_name, style)
        result = subprocess.run(cmd, cwd=pv_env.TMP_DIR, capture_output=True, text=True)
    finally:
        os.remove(ass_path)
    if result.returncode != 0:
        raise RuntimeError("字幕烧录失败:\n{}".format(result.stderr[-1500:]))

    print("[subtitle_render] 完成 -> {}".format(output_path))
    return output_path
