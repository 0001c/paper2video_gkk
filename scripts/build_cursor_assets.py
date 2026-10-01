"""生成本项目原创透明光标；仅使用已有 Pillow，保留原版 red.png。"""
from pathlib import Path
from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1] / 'Paper2Video-main/src/cursor_image'


def draw_style(style):
    scale = 4
    image = Image.new('RGBA', (96 * scale, 96 * scale))
    draw = ImageDraw.Draw(image)
    def ellipse(box, fill=None, outline=None, width=1):
        draw.ellipse(tuple(v * scale for v in box), fill=fill, outline=outline, width=width * scale)
    def line(points, fill, width):
        draw.line([(x * scale, y * scale) for x, y in points], fill=fill, width=width * scale, joint='curve')
    def polygon(points, fill, outline, width=3):
        scaled = [(x * scale, y * scale) for x, y in points]
        draw.polygon(scaled, fill=fill)
        draw.line([*scaled, scaled[0]], fill=outline, width=width * scale, joint='curve')
    if style.startswith('laser-'):
        color = (239, 68, 68) if style == 'laser-red' else (37, 124, 246)
        for radius, alpha in ((38, 22), (29, 45), (21, 90)):
            ellipse((48 - radius, 48 - radius, 48 + radius, 48 + radius), (*color, alpha))
        ellipse((36, 36, 60, 60), (*color, 255), (255, 255, 255, 240), 3)
        ellipse((43, 42, 49, 48), (255, 255, 255, 220))
    elif style == 'ring-gold':
        ellipse((18, 18, 78, 78), outline=(41, 31, 13, 220), width=11)
        ellipse((18, 18, 78, 78), outline=(251, 191, 36, 255), width=7)
        ellipse((28, 28, 68, 68), outline=(255, 255, 255, 230), width=2)
    elif style == 'cross-teal':
        ellipse((28, 28, 68, 68), outline=(8, 47, 49, 220), width=6)
        ellipse((28, 28, 68, 68), outline=(45, 212, 191, 255), width=3)
        for points in ([(48, 10), (48, 34)], [(48, 62), (48, 86)], [(10, 48), (34, 48)], [(62, 48), (86, 48)]):
            line(points, (8, 47, 49, 240), 7)
            line(points, (45, 212, 191, 255), 3)
        ellipse((44, 44, 52, 52), (255, 255, 255, 255), (8, 47, 49, 255), 2)
    elif style == 'arrow-white':
        polygon([(23, 21), (23, 77), (37, 65), (48, 86), (62, 79), (51, 59), (74, 59)], (0, 0, 0, 55), (0, 0, 0, 55), 5)
        polygon([(20, 16), (20, 72), (34, 60), (45, 81), (59, 74), (48, 54), (71, 54)], '#ffffff', '#182230', 4)
        line([(26, 32), (26, 57)], '#d6e4f0', 3)
    elif style == 'hand-white':
        polygon([(34, 54), (34, 20), (37, 14), (43, 14), (47, 20), (47, 44), (53, 39), (59, 42), (65, 40), (71, 44), (77, 44), (81, 50), (80, 67), (71, 84), (44, 84), (35, 73), (19, 57), (19, 51), (24, 48)], '#ffffff', '#182230', 3)
        for x, y in ((53, 48), (63, 48), (73, 51)):
            line([(x, y), (x, 63)], '#8a9baa', 2)
        line([(44, 79), (70, 79)], '#d6e4f0', 3)
    return image.resize((96, 96), Image.Resampling.LANCZOS)


if __name__ == '__main__':
    for style in ('laser-red', 'laser-blue', 'ring-gold', 'cross-teal', 'arrow-white', 'hand-white'):
        draw_style(style).save(ROOT / (style + '.png'))
    print('已生成 6 套透明视频光标，原版素材保留')
