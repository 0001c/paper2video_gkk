"""生成、审核、配音和定位共用的讲稿契约；不加载模型。"""

import json
import re


def _load_json(text):
    """只兼容字符串内换行，以及 cursor_prompt 的裸 no；不补缺失内容。"""
    while True:
        try:
            return json.loads(text, strict=False)
        except json.JSONDecodeError as error:
            # 只在解析器指出的字段值位置替换，避免改写口播中的原文。
            if (re.search(r'"cursor_prompt"\s*:\s*$', text[:error.pos])
                    and re.match(r'no\s*[,}]', text[error.pos:])):
                text = text[:error.pos] + '"no"' + text[error.pos + 2:]
                continue
            raise


def _structured_field(value, field):
    if not isinstance(value, str):
        return value  # 交给共用的非空字符串校验，不将数组/对象猜成文本。
    if any(ord(char) < 32 and char not in '\n\r\t' for char in value):
        raise ValueError(field + ' 含不支持的控制字符')
    value = re.sub(r'[\n\r\t]+', ' ', value).strip()
    if field == 'cursor_prompt' and '|' in value:
        # 多个带引号的目标之间的 | 是旧输出分隔符；目标内部的数学 | 保留。
        quoted = r'"[^"\n]*"'
        if re.fullmatch(quoted + r'(?:\s*(?:\|\s*)?' + quoted + r')*', value):
            value = re.sub(r'"\s*\|\s*"', '" "', value)
        elif '"' not in value:
            value = '"' + value + '"'
        else:
            raise ValueError('cursor_prompt 含有歧义的分隔符，请只返回原文短语或 no')
    return value


def _split_line(line):
    """取引号外最后一个分隔符，避免把目标 p(y|x) 拆进口播。"""
    quoted, escaped, separator = False, False, None
    for index, char in enumerate(line):
        if char == '"' and not escaped:
            quoted = not quoted
        elif char == '|' and not quoted:
            separator = index
        escaped = char == '\\' and not escaped
    if separator is None:
        return None
    return line[:separator], line[separator + 1:]


def parse_script(text):
    text = str(text).strip()
    fenced = re.fullmatch(r'```(?:json)?\s*\n(.*?)\n\s*```', text, re.S | re.I)
    if fenced:
        text = fenced[1].strip()
    if text.startswith('{'):
        value = _load_json(text)
        if not isinstance(value, dict) or set(value) != {'pages'} or not isinstance(value['pages'], list):
            raise ValueError('讲稿 JSON 必须为 pages 数组')
        pages = []
        for page in value['pages']:
            if not isinstance(page, dict) or set(page) != {'sentences'} or not isinstance(page['sentences'], list):
                raise ValueError('每页必须包含 sentences 数组')
            lines = []
            for sentence in page['sentences']:
                if not isinstance(sentence, dict) or set(sentence) != {'speech_text', 'cursor_prompt'}:
                    raise ValueError('每句必须包含 speech_text 和 cursor_prompt')
                lines.append([_structured_field(sentence['speech_text'], 'speech_text'),
                              _structured_field(sentence['cursor_prompt'], 'cursor_prompt')])
            pages.append(lines)
    else:
        pages = []
        for number, page in enumerate(re.split(r'(?m)^\s*###\s*$', text), 1):
            if not page.strip():
                continue
            lines, pending = [], []
            for line in page.splitlines():
                if not line.strip():
                    continue
                if lines and lines[-1][1].count('"') % 2:
                    lines[-1][1] += ' ' + line.strip()
                    continue
                split = _split_line(line)
                if split is None:
                    pending.append(line.strip())
                    continue
                speech, cursor = split
                if (speech.count('(') > speech.count(')') or speech.count('（') > speech.count('）')) and not cursor.strip().startswith('"') and cursor.strip().lower() != 'no':
                    pending.append(line.strip())
                    continue
                if not speech.strip() and not pending and lines:
                    # 旧模型会把同一段的多个目标各写一行；保留引用但不重复配音。
                    lines[-1][1] += ' ' + cursor.strip()
                    continue
                lines.append([' '.join([*pending, speech.strip()]).strip(), cursor.strip()])
                pending = []
            if pending:
                speech = ' '.join(pending)
                if not lines or speech[-1] not in '。！？.!?':
                    raise ValueError('第 {} 页有未绑定光标目标的讲稿（疑似截断）'.format(number))
                lines.append([speech, 'no'])
            pages.append(lines)
    if not pages:
        raise ValueError('讲稿为空')
    for number, lines in enumerate(pages, 1):
        if not lines:
            raise ValueError('第 {} 页没有讲稿'.format(number))
        for speech, cursor in lines:
            if not isinstance(speech, str) or not speech.strip() or not isinstance(cursor, str) or not cursor.strip():
                raise ValueError('第 {} 页每句都需要非空讲稿与光标目标（无需光标写 no）'.format(number))
            if any(ord(char) < 32 for char in speech + cursor):
                raise ValueError('讲稿句子或光标目标包含未规范化的换行或控制字符')
            if '|' in cursor and not re.fullmatch(r'"[^"\n]*"(?:\s*"[^"\n]*")*', cursor):
                raise ValueError('光标目标中的分隔符 | 必须位于完整引号内')
            if cursor.count('"') % 2:
                raise ValueError('光标目标引号不成对（疑似截断）')
    return pages


def normalize_script(text):
    return '\n###\n'.join('\n'.join(speech.strip() + ' | ' + cursor.strip() for speech, cursor in page)
                          for page in parse_script(text))


def validate_script_output(text, expected_slides):
    try:
        pages = parse_script(text)
        if len(pages) != expected_slides:
            raise ValueError('页数 {} 与幻灯片张数 {} 不一致'.format(len(pages), expected_slides))
        return True, ''
    except (ValueError, TypeError) as exc:
        return False, str(exc)
