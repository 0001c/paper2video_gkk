"""逐请求用量账本；官方北京按量价估算，不冒充账户账单。"""

import json
import os
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlsplit


PRICE_SOURCE = 'https://help.aliyun.com/zh/model-studio/model-pricing'
PRICE_VERSION = '2026-10-03-beijing-standard-v2'
TOKEN_PRICES = {
    'qwen3.8-flash': [(1000000, '0.8', '2.7')],
    'qwen3.8-max': [(1000000, '12', '36')],
    'qwen3-vl-plus': [(32000, '1', '10'), (128000, '1.5', '15'), (256000, '3', '30')],
}
CHAR_PRICES = {'cosyvoice-v3.5-plus': '1.5', 'cosyvoice-v3.5-flash': '0.8',
               'cosyvoice-v3-plus': '2', 'cosyvoice-v3-flash': '1', 'cosyvoice-v2': '2'}
ROLES = ('text', 'vision', 'tts', 'asr', 'grounding', 'avatar', 'voice')


def write_record(directory, record):
    root = Path(directory) / 'model_usage'
    root.mkdir(parents=True, exist_ok=True)
    filename = root / (record['id'] + '.json')
    temporary = filename.with_suffix('.tmp')
    temporary.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(filename)


def begin(role, model, endpoint, directory=None):
    directory = directory or os.environ.get('PAPER2VIDEO_USAGE_DIR')
    if not directory:
        return None
    hostname = urlsplit(endpoint).hostname or ''
    record = {'id': uuid.uuid4().hex, 'role': role, 'model': model,
              'started_at': datetime.now(timezone.utc).isoformat(), 'state': 'submission_unknown',
              'region': 'cn-beijing' if hostname == 'dashscope.aliyuncs.com' or hostname.endswith('.cn-beijing.maas.aliyuncs.com') else 'unknown',
              'usage': {}, 'usage_source': 'unknown', 'estimated_cny': None,
              'price_version': PRICE_VERSION, 'price_source': PRICE_SOURCE}
    write_record(directory, record)
    return directory, record


def estimate(record):
    if record['region'] != 'cn-beijing' or record['state'] != 'succeeded':
        return None
    usage = record['usage']
    model = record['model']
    if model in TOKEN_PRICES and all(type(usage.get(name)) is int and usage[name] >= 0 for name in ('prompt_tokens', 'completion_tokens')):
        for ceiling, input_price, output_price in TOKEN_PRICES[model]:
            if usage['prompt_tokens'] <= ceiling:
                return float((Decimal(usage['prompt_tokens']) * Decimal(input_price) + Decimal(usage['completion_tokens']) * Decimal(output_price)) / 1000000)
    if model in CHAR_PRICES and type(usage.get('characters')) is int and usage['characters'] >= 0:
        return float(Decimal(usage['characters']) * Decimal(CHAR_PRICES[model]) / 10000)
    return None


def finish(ticket, usage=None, state='succeeded', source='provider'):
    if ticket is None:
        return
    directory, record = ticket
    clean = {name: usage[name] for name in ('prompt_tokens', 'completion_tokens', 'total_tokens', 'characters', 'cached_tokens')
             if type((usage or {}).get(name)) is int and usage[name] >= 0}
    record.update(state=state, usage=clean, usage_source=source if clean else 'unknown',
                  finished_at=datetime.now(timezone.utc).isoformat())
    record['estimated_cny'] = estimate(record)
    write_record(directory, record)


def summary(directory):
    root = Path(directory) / 'model_usage'
    groups = {role: {'role': role, 'requests': 0, 'unknown_requests': 0, 'estimated_usage_requests': 0, 'estimated_cny': 0.0, 'models': [], 'usage': {}} for role in ROLES}
    for filename in sorted(root.glob('*.json')):
        try:
            record = json.loads(filename.read_text(encoding='utf-8'))
            group = groups[record['role']]
        except (OSError, ValueError, KeyError):
            continue
        group['requests'] += 1
        if record.get('usage_source') == 'submitted_text_estimate':
            group['estimated_usage_requests'] += 1
        for name, count in record.get('usage', {}).items():
            group['usage'][name] = group['usage'].get(name, 0) + count
        if record['model'] not in group['models']:
            group['models'].append(record['model'])
        if record.get('estimated_cny') is None:
            group['unknown_requests'] += 1
        else:
            group['estimated_cny'] += record['estimated_cny']
    for group in groups.values():
        group['estimated_cny'] = round(group['estimated_cny'], 8)
    return {'currency': 'CNY', 'price_version': PRICE_VERSION, 'price_source': PRICE_SOURCE,
            'stages': list(groups.values()), 'known_estimated_cny': round(sum(group['estimated_cny'] for group in groups.values()), 8),
            'unknown_requests': sum(group['unknown_requests'] for group in groups.values()),
            'notice': '标准按量价估算，不含免费额度、缓存/Batch折扣、合同价、OSS与本地算力；未接入/未调用不代表实际账单为零。续跑共享产物目录按该目录累计。'}
