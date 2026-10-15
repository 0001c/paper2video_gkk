"""按实际口播计数的修订清单；只给删改目标，不截断讲稿。"""
import hashlib
import math
import re

from script_validation import parse_script
from spoken_text import plain_text, prepare_spoken


def oral_units(text, language='zh'):
    spoken = prepare_spoken(text, language)[0]
    return len(re.findall(r'[\u3400-\u9fff]', spoken)) + len(re.findall(r'[A-Za-z0-9]+', spoken))


def oral_fingerprint(text, language='zh'):
    speech = '\n'.join(line[0] for page in parse_script(text) for line in page)
    # 不把标点、空白和光标目标的变化当成口播修订。
    try:
        speech = prepare_spoken(speech, language)[0]
    except ValueError:
        pass  # 不支持的公式仍由审核预检拦截；指纹不阻止保存待修候选。
    return hashlib.sha256(plain_text(speech).encode('utf-8')).hexdigest()


def paragraph_targets(text, budget, rate, language='zh'):
    paragraphs = [speech for page in parse_script(text) for speech, _ in page]
    rows = []
    for i, speech in enumerate(paragraphs, 1):
        try:
            units = oral_units(speech, language)
        except ValueError:
            return []
        protected = bool(re.search(r'=|等于|因此|所以|结果|推导|梯度|形状|shape|因为', speech, re.I))
        rows.append({'paragraph': i, 'units': units, 'preserve_reasoning': protected,
                     'opening': speech[:80], 'target_units': units})
    total = sum(row['units'] for row in rows)
    target = max(1, math.floor(budget * rate))
    if total <= target:
        return rows
    # 必要推导段优先保留；在段落原长度内分配预算，目标总和等于全页目标。
    weights = [row['units'] * (2 if row['preserve_reasoning'] else 1) for row in rows]
    allocation = [0] * len(rows)
    for _ in range(target):
        candidates = [i for i, row in enumerate(rows) if allocation[i] < row['units']]
        if not candidates:
            break
        best = max(candidates, key=lambda i: weights[i] / (allocation[i] + 1))
        allocation[best] += 1
    for row, amount in zip(rows, allocation):
        row['target_units'] = amount
    return rows


def compression_prompt(rows):
    if not rows:
        return ''
    lines = [f'第{r["paragraph"]}段（开头“{r["opening"]}”）：{r["units"]}→{r["target_units"]}单位；'
             + ('保留条件、推导与结论，压缩措辞' if r['preserve_reasoning'] else '优先删重复铺垫、类比或小结') for r in rows]
    return '\n【逐段删减目标，以口播展开后计数】\n' + '\n'.join(lines) + '\n若无法保留必要推导，请留待审核；不得机械截断、隐去结果或用省略号充数。'
