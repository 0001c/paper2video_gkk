"""视频光标的统一素材目录、热点与公开选项。"""
from pathlib import Path

ASSETS = Path(__file__).resolve().parent / 'cursor_image'
DEFAULT = 'classic-red'  # 旧任务与 CLI 保持原红色箭头。
STYLES = {
    'laser-red': ('红色光点', '醒目指示，适合课堂讲解', (0.5, 0.5)),
    'laser-blue': ('蓝色光点', '冷色强调，适合图表演示', (0.5, 0.5)),
    'ring-gold': ('金色圆环', '空心标记，保留文字可读性', (0.5, 0.5)),
    'cross-teal': ('青色准星', '精确定位公式与图中细节', (0.5, 0.5)),
    'arrow-white': ('白色箭头', '深色描边，适合操作讲解', (20 / 96, 16 / 96)),
    'hand-white': ('手型指针', '指尖指示，适合步骤演示', (40 / 96, 14 / 96)),
    'classic-red': ('经典红箭头', '沿用原版视频光标', (0.5, 0.5)),
    'none': ('不显示光标', '保留配音与字幕，隐藏指示标记', (0.5, 0.5)),
}


def resolve(style):
    if not isinstance(style, str) or style not in STYLES:
        raise ValueError('cursor_style：请选择有效的视频光标样式')
    file = None if style == 'none' else ASSETS / ('red.png' if style == DEFAULT else style + '.png')
    return file, STYLES[style][2]


def options():
    return [{'value': key, 'label': value[0], 'description': value[1],
             'image_url': None if key == 'none' else '/api/cursors/' + key + '.png'}
            for key, value in STYLES.items()]


def normalize(config):
    style = config.get('cursor_style', DEFAULT)
    resolve(style)
    size = config.get('cursor_scale', 1.0)
    if isinstance(size, bool) or not isinstance(size, (float, int)) or size not in (0.75, 1, 1.5):
        raise ValueError('cursor_scale：请选择小、标准或大尺寸')
    config.update(cursor_style=style, cursor_scale=float(size))
