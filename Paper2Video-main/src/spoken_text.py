"""显示原文到口播的确定性映射；不删除脚注样式数字，不猜教材事实。"""

import re
import unicodedata

NORMALIZATION_VERSION = 1
_DIGITS = '零一二三四五六七八九'
_READINGS = {'α': '阿尔法', 'β': '贝塔', 'γ': '伽马', 'δ': '德尔塔', 'η': '伊塔',
             'θ': '西塔', 'λ': '兰姆达', 'μ': '缪', 'σ': '西格玛', 'ε': '艾普西龙',
             '∂': '偏导', '∇': '梯度', '+': '加', '=': '等于', '×': '乘以', '÷': '除以',
             '≤': '小于等于', '≥': '大于等于', '^': '的次方', '→': '到', '*': '乘以',
             '/': '除以', '<': '小于', '>': '大于', '_': '下标', '²': '平方', '³': '立方',
             '∑': '求和', '∏': '连乘', '√': '平方根', '∞': '无穷', '∈': '属于', '|': '竖线'}
_READINGS.update({chr(0x2080 + i): '下标' + value for i, value in enumerate(_DIGITS)})
_TOKEN = re.compile(r'\\[A-Za-z]+|\d+(?:\.\d+)?%?|[' + re.escape(''.join(_READINGS)) + r'\-]')
_LATEX = {'alpha': 'α', 'beta': 'β', 'gamma': 'γ', 'delta': 'δ', 'eta': 'η',
          'theta': 'θ', 'lambda': 'λ', 'mu': 'μ', 'sigma': 'σ', 'epsilon': 'ε',
          'partial': '∂', 'nabla': '∇', 'times': '×', 'div': '÷', 'leq': '≤', 'geq': '≥'}


def plain_text(text):
    return ''.join(character.casefold() for character in unicodedata.normalize('NFKC', text)
                   if character.isalnum())


def _number(value, digits=False):
    if '.' in value:
        integer, fraction = value.split('.')
        return _number(integer, digits) + '点' + ''.join(_DIGITS[int(c)] for c in fraction)
    if digits or len(value) > 4 or value.startswith('0'):
        return ''.join(_DIGITS[int(c)] for c in value)
    out = ''
    for index, char in enumerate(value):
        unit = len(value) - index - 1
        if char == '0':
            if out and not out.endswith('零') and any(c != '0' for c in value[index + 1:]):
                out += '零'
        else:
            out += _DIGITS[int(char)] + ('', '十', '百', '千')[unit]
    return '十' + out[2:] if out.startswith('一十') else out


def prepare_spoken(text, language='zh'):
    if not isinstance(text, str) or not plain_text(text):
        raise ValueError('口播文字为空')
    if any(ord(c) < 32 and c not in '\n\r\t' for c in text):
        raise ValueError('口播含未识别控制字符，请先核对源文')
    output, mapping = [], []
    def append(value, first, last):
        output.append(value)
        for char in value:
            normalized = plain_text(char)
            if normalized:
                mapping.append({'original_index': first, 'original_end': last, 'normalized': normalized})
    cursor = 0
    matches = list(_TOKEN.finditer(text)) if language == 'zh' else []
    for match in matches:
        for index in range(cursor, match.start()):
            append(text[index], index, index + 1)
        token = match.group()
        if token.startswith('\\'):
            name = token[1:]
            if name not in _LATEX:
                raise ValueError('口播公式包含未支持命令 \\{}，请提供明确口述后再合成'.format(name))
            value = _READINGS[_LATEX[name]]
        elif token[0].isdecimal():
            value = ('百分之' if token.endswith('%') else '') + _number(token.rstrip('%'), text[match.end():].startswith('年'))
        elif token == '-':
            left, right = text[:match.start()], text[match.end():]
            if re.search(r'[A-Za-z]{2}$', left) and re.match(r'[A-Za-z]{2}', right):
                value = '-'
            else:
                value = '减' if left.rstrip() and left.rstrip()[-1].isalnum() else '负'
        else:
            value = _READINGS[token]
        append(value, match.start(), match.end())
        cursor = match.end()
    for index in range(cursor, len(text)):
        append(text[index], index, index + 1)
    return ''.join(output), mapping
