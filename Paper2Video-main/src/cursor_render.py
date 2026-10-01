import hashlib
import json
import math
from pathlib import Path
import shutil
import subprocess
import tempfile
from functools import lru_cache
from PIL import Image
import pv_env


@lru_cache(maxsize=1)
def filter_file_option():
    # FFmpeg 9 移除了旧参数；兼容本机新版本及原有旧版安装。
    help_text = subprocess.run(['ffmpeg', '-h', 'full'], capture_output=True, check=True).stdout
    return '-filter_complex_script' if b'-filter_complex_script ' in help_text else '-/filter_complex'


def cursor_overlay(filename, size):
    """按素材内容与尺寸缓存透明叠图，不改源素材，不混用同尺寸的不同样式。"""
    source = Path(filename)
    key = hashlib.sha256(source.read_bytes() + str(size).encode()).hexdigest()[:20]
    root = Path(pv_env.TMP_DIR)
    root.mkdir(parents=True, exist_ok=True)
    target = root / ('cursor_overlay_' + key + '.png')
    if not target.is_file():
        with tempfile.NamedTemporaryFile(dir=root, suffix='.png', delete=False) as temporary:
            temp = Path(temporary.name)
        try:
            with Image.open(source) as image:
                image.convert('RGBA').resize((size, size), Image.Resampling.LANCZOS).save(temp)
            temp.replace(target)
        finally:
            temp.unlink(missing_ok=True)
    return str(target)


def render_cursor_on_video(input_video, output_video, cursor_points, transition_duration=0.1,
                           cursor_size=10, cursor_img_path='cursor.png', cursor_hotspot=(0.5, 0.5)):
    if not cursor_img_path or not cursor_points:
        if Path(input_video).resolve() != Path(output_video).resolve():
            shutil.copy2(input_video, output_video)
        return
    if not math.isfinite(transition_duration) or transition_duration <= 0 or cursor_size < 1:
        raise ValueError('光标尺寸和移动时间必须为正数')
    points = {}
    for t, x, y in cursor_points:
        if not all(isinstance(v, (float, int)) and math.isfinite(v) for v in (t, x, y)):
            raise ValueError('光标坐标和时间必须为有限数字')
        points[max(0, t)] = (x, y)
    ordered = [(t, *xy) for t, xy in sorted(points.items())]
    overlay = cursor_overlay(cursor_img_path, int(cursor_size))
    # 单个 overlay 表达式保持最后一个目标，只有一个目标时也能生成视频。
    def position(axis):
        offset = cursor_size * cursor_hotspot[axis - 1]
        terms = [str(ordered[0][axis] - offset)]
        for a, b in zip(ordered, ordered[1:]):
            if a[axis] == b[axis]:
                continue
            start = max(a[0], b[0] - transition_duration)
            terms.append('({})*clip((t-{})/{},0,1)'.format(b[axis] - a[axis], start, b[0] - start))
        # 平衡加法树避免 FFmpeg 在上千个目标时触发表达式递归深度限制。
        while len(terms) > 1:
            terms = ['({}+{})'.format(terms[i], terms[i + 1]) if i + 1 < len(terms) else terms[i]
                     for i in range(0, len(terms), 2)]
        return terms[0]
    filters = "[0:v][1:v]overlay=x='{}':y='{}':enable='gte(t,{})':eval=frame[vout]".format(position(1), position(2), ordered[0][0])
    # 长课可有上千个目标，Windows 命令行有长度上限，滤镜写入临时文件。
    with tempfile.NamedTemporaryFile(dir=pv_env.TMP_DIR, suffix='.ffscript', mode='w', encoding='utf-8', delete=False) as handle:
        handle.write(filters)
        filter_file = Path(handle.name)
    try:
        subprocess.run(['ffmpeg', '-y', '-v', 'error', '-i', input_video, '-i', overlay,
                        filter_file_option(), str(filter_file), '-map', '[vout]', '-map', '0:a?',
                        '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-c:a', 'copy', output_video], check=True)
    finally:
        filter_file.unlink(missing_ok=True)
    print('[cursor] 光标已合成：' + output_video)


def render_video_with_cursor_from_json(video_path, out_video_path, json_path, cursor_img_path,
                                      transition_duration=0.1, cursor_size=16, cursor_hotspot=(0.5, 0.5)):
    if not cursor_img_path:
        render_cursor_on_video(video_path, out_video_path, [], cursor_img_path=None)
        return
    with open(json_path, encoding='utf-8') as handle:
        data = json.load(handle)
    points = [(slide['start'] + (0.5 if i else 0), *slide['cursor']) for i, slide in enumerate(data)]
    render_cursor_on_video(video_path, out_video_path, points, transition_duration, cursor_size,
                           cursor_img_path, cursor_hotspot)
