"""逐请求用量账本；官方北京按量价估算，不冒充账户账单。"""

import json
import os
import re
import sys
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlsplit


PRICE_SOURCE = 'https://help.aliyun.com/zh/model-studio/model-pricing'
PRICE_VERSION = '2026-10-07-beijing-standard-v3'
TOKEN_PRICES = {
    'qwen3.8-flash': [(1000000, '0.8', '2.7')],
    'qwen3.8-max': [(1000000, '12', '36')],
    'qwen3-vl-plus': [(32000, '1', '10'), (128000, '1.5', '15'), (256000, '3', '30')],
}
CHAR_PRICES = {'cosyvoice-v3.5-plus': '1.5', 'cosyvoice-v3.5-flash': '0.8',
               'cosyvoice-v3-plus': '2', 'cosyvoice-v3-flash': '1', 'cosyvoice-v2': '2'}
AVATAR_VIDEO_PRICES = {480: '0.5', 720: '0.9'}
INTEGER_USAGE = ('prompt_tokens', 'completion_tokens', 'total_tokens', 'characters', 'cached_tokens')
ROLES = ('text', 'vision', 'tts', 'asr', 'grounding', 'avatar', 'voice', 'qa')


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


def restore(directory, record_id):
    """恢复已持久化的请求账本；续查远程任务不会新建收费请求。"""
    if not isinstance(record_id, str) or not re.fullmatch(r'[0-9a-f]{32}', record_id):
        raise ValueError('费用记录ID无效')
    try:
        record = json.loads((Path(directory) / 'model_usage' / (record_id + '.json')).read_text(encoding='utf-8'))
        if (record.get('id') != record_id or record.get('role') not in ROLES
                or any(not isinstance(record.get(name), str) for name in ('model', 'region', 'price_version', 'price_source', 'state', 'started_at'))
                or not isinstance(record.get('usage'), dict)):
            raise ValueError('费用记录内容无效')
    except (OSError, ValueError, AttributeError) as exc:
        raise ValueError('费用记录不存在或无效，禁止新建重复账单') from exc
    return directory, record


def clean_usage(usage):
    usage = usage if isinstance(usage, dict) else {}
    clean = {name: usage[name] for name in INTEGER_USAGE
             if type(usage.get(name)) is int and usage[name] >= 0}
    if type(usage.get('images')) is int and usage['images'] > 0:
        clean['images'] = usage['images']
    duration = usage.get('duration_seconds')
    if type(duration) in (int, float) and 0 < duration <= sys.float_info.max:
        clean['duration_seconds'] = duration
    if type(usage.get('resolution')) is int and usage['resolution'] in AVATAR_VIDEO_PRICES:
        clean['resolution'] = usage['resolution']
    return clean


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
    if model == 'wan2.2-s2v-detect' and type(usage.get('images')) is int and usage['images'] > 0:
        return float(Decimal(usage['images']) * Decimal('0.004'))
    if model == 'wan2.2-s2v' and type(usage.get('resolution')) is int and usage['resolution'] in AVATAR_VIDEO_PRICES:
        duration = usage.get('duration_seconds')
        if type(duration) in (int, float) and 0 < duration <= sys.float_info.max:
            return float(Decimal(str(duration)) * Decimal(AVATAR_VIDEO_PRICES[usage['resolution']]))
    return None


def finish(ticket, usage=None, state='succeeded', source='provider'):
    if ticket is None:
        return
    directory, record = ticket
    clean = clean_usage(usage)
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
        for name, count in clean_usage(record.get('usage')).items():
            if name == 'resolution':
                continue
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
