"""Shared subtitle settings for HTTP snapshots, CLI and ASS rendering."""

import math
import re

DEFAULTS = {
    'enabled': True, 'font_name': '', 'font_size': 32,
    'text_color': '#FFFFFF', 'background_color': '#000000',
    'background_opacity': 0.55, 'margin_horizontal': 5, 'margin_bottom': 3, 'line_spacing': 1.6,
}


def normalize(value=None):
    if value is None:
        return dict(DEFAULTS)
    if not isinstance(value, dict):
        raise ValueError('subtitle_style：必须是对象')
    unknown = set(value) - set(DEFAULTS)
    if unknown:
        raise ValueError('subtitle_style.' + sorted(unknown)[0] + '：未知字段')
    style = {**DEFAULTS, **value}
    if not isinstance(style['enabled'], bool):
        raise ValueError('subtitle_style.enabled：必须是布尔值')
    font = style['font_name']
    if not isinstance(font, str) or len(font) > 80 or any(ord(char) < 32 or char in ',\\{}' for char in font):
        raise ValueError('subtitle_style.font_name：字体名最多80字，不能包含控制字符或ASS分隔符')
    if '/' in font or re.match(r'^[a-z][a-z0-9+.-]*:', font.strip(), re.I):
        raise ValueError('subtitle_style.font_name：请填写字体名称或留空自动选择，不能填写模型接口URL或文件路径')
    style['font_name'] = font.strip()
    for field in ('text_color', 'background_color'):
        if not isinstance(style[field], str) or not re.fullmatch(r'#[0-9a-fA-F]{6}', style[field]):
            raise ValueError('subtitle_style.' + field + '：必须为#RRGGBB颜色')
        style[field] = style[field].upper()
    for field, lower, upper in (('font_size', 16, 72), ('background_opacity', 0, 1),
                                ('margin_horizontal', 2, 20), ('margin_bottom', 1, 15), ('line_spacing', 1.6, 2.4)):
        number = style[field]
        if isinstance(number, bool) or not isinstance(number, (int, float)) or not math.isfinite(number) or not lower <= number <= upper:
            raise ValueError('subtitle_style.' + field + '：必须为{}～{}之间的有限数值'.format(lower, upper))
    if int(style['font_size']) != style['font_size']:
        raise ValueError('subtitle_style.font_size：必须为整数')
    style['font_size'] = int(style['font_size'])
    return style
