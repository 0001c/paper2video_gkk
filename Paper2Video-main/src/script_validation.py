"""生成、审核、配音和定位共用的讲稿契约；不加载模型。"""

import json
import re


def parse_script(text):
    text = str(text).strip()
    if text.startswith('```json\n') and text.endswith('\n```'):
        text = text[8:-4].strip()
    if text.startswith('{'):
        value = json.loads(text)
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
                lines.append([sentence['speech_text'], sentence['cursor_prompt']])
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
                if '|' not in line:
                    pending.append(line.strip())
                    continue
                speech, cursor = line.rsplit('|', 1)
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
            if any(char in speech + cursor for char in ('\n', '\r')) or '|' in cursor:
                raise ValueError('结构化句子不能包含换行，光标目标不能含分隔符 |')
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
