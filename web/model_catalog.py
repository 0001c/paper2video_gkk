import json
import uuid
from collections import OrderedDict
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import Field

from web.model_config import ConnectionID, ConfigurationError, StrictModel
from typing import Literal


ROLES = ('text', 'vision', 'tts', 'asr', 'grounding', 'avatar')
RECOMMENDED = dict(zip(ROLES, ('qwen3.8-flash', 'qwen3-vl-plus', 'cosyvoice-v3.5-plus',
                              'fun-asr', 'qwen3-vl-plus', 'wan2.2-s2v')))
LOCAL = {'tts': ('qwen3', 'qwen3tts'), 'asr': ('qwen3', 'qwen3-asr'),
         'grounding': ('gui_owl', 'gui-owl'), 'avatar': ('musetalk', 'musetalk')}
SNAPSHOT = json.loads((Path(__file__).resolve().parents[1] / 'docs' / 'bailian-model-catalog-2026-10-02.json').read_text(encoding='utf-8'))


class CatalogRequest(StrictModel):
    connection_id: ConnectionID
    role: Literal['all', 'text', 'vision', 'tts', 'asr', 'grounding', 'avatar']
    include_retiring: bool = False
    cursor: str | None = Field(default=None, max_length=100)
    limit: int = Field(default=100, ge=1, le=100)
    refresh: bool = False


def model_id(value):
    return isinstance(value, str) and 0 < len(value) <= 200 and not any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in value)


def prices(raw):
    """保留官方价格的分段、单位和时段；缺失/非法单价不能展示为免费。"""
    groups = []
    for group in raw if isinstance(raw, list) else []:
        if not isinstance(group, dict):
            continue
        rows = []
        for row in group.get('prices', []) if isinstance(group.get('prices'), list) else []:
            if not isinstance(row, dict):
                continue
            try:
                amount = Decimal(str(row.get('price')))
                if not amount.is_finite() or amount < 0:
                    continue
            except InvalidOperation:
                continue
            rows.append({**{key: str(row.get(key) or '')[:500] for key in ('type', 'price_name', 'price_unit', 'time_band')},
                         'price': str(amount)})
        if rows:
            groups.append({'range_name': str(group.get('range_name') or 'Default')[:500], 'prices': rows})
    return groups


def merged(compatible, native):
    records = {}
    for raw in native:
        identity = raw.get('model') or raw.get('id')
        if not model_id(identity):
            continue
        info = raw.get('model_info') if isinstance(raw.get('model_info'), dict) else {}
        limits = {name: info[name] for name in ('context_window', 'max_input_tokens', 'max_output_tokens')
                  if isinstance(info.get(name), int) and not isinstance(info[name], bool) and info[name] > 0}
        records[identity] = {'id': identity, 'model_info': limits, 'capabilities': [value for value in raw.get('capabilities', []) if isinstance(value, str)],
                             'sources': ['native'], 'offline_time': (raw.get('inference_offline_info') or {}).get('offline_time'),
                             'name': str(raw.get('name') or identity)[:200], 'prices': prices(raw.get('prices')),
                             'model_provider': str(raw.get('provider') or '')[:100],
                             'inference_provider': str(raw.get('inference_provider') or '')[:100]}
    for identity in compatible:
        if not model_id(identity):
            continue
        record = records.setdefault(identity, {'id': identity, 'capabilities': [], 'sources': [], 'offline_time': None})
        record['sources'].append('compatible')
    return list(records.values())


def candidate(record, role, provider, today=None):
    caps = record['capabilities']
    compatible = 'compatible' in record['sources']
    known = {'text': 'TG' in caps, 'vision': 'VU' in caps, 'tts': 'TTS' in caps,
             'asr': 'ASR' in caps, 'grounding': 'VU' in caps or record['id'] == RECOMMENDED['grounding'],
             'avatar': record['id'] == RECOMMENDED['avatar']}[role]
    unknown = role in ('text', 'vision') and compatible and not caps
    if not known and not unknown:
        return None
    speech = role == 'tts' and 'native' in record['sources'] and record['id'] in (
        'cosyvoice-v3.5-plus', 'cosyvoice-v3.5-flash', 'cosyvoice-v3-plus', 'cosyvoice-v3-flash', 'cosyvoice-v2')
    avatar = role == 'avatar' and 'native' in record['sources'] and record['id'] == 'wan2.2-s2v'
    implemented = (role in ('text', 'vision') and compatible) or (role == 'grounding' and compatible and record['id'] == RECOMMENDED['grounding']) or speech or avatar
    offline = record['offline_time']
    status = 'active'
    if offline:
        try:
            cutoff = datetime.fromisoformat(offline).date()
            status = 'retired' if cutoff <= (today or datetime.now(timezone.utc).date()) else 'retiring'
        except (ValueError, TypeError):
            status = 'lifecycle_unknown'
    return {**record, 'role': role, 'roles': [value for value in ROLES if value == role or
            (value == 'text' and 'TG' in caps) or (value == 'vision' and 'VU' in caps)],
            'protocols': ['bailian_native_async_avatar'] if avatar else ['bailian_http_sse_tts'] if speech else ['openai_compatible_chat'] if implemented else ['bailian_native'],
            'adapter_implemented': implemented, 'selectable': implemented and status not in ('retired', 'lifecycle_unknown'),
            'status': status, 'verification': 'capability_unknown' if unknown else 'catalog_only',
            'recommended': record['id'] == RECOMMENDED[role] and status == 'active', 'provider': provider}


def directory_get(client, url, params=None):
    with client.stream('GET', url, params=params) as response:
        response.raise_for_status()
        body = bytearray()
        for chunk in response.iter_bytes():
            if len(body) + len(chunk) > 5 * 1024 * 1024:
                raise ValueError('目录响应超过大小限制')
            body.extend(chunk)
    value = json.loads(body)
    if not isinstance(value, dict):
        raise ValueError('目录响应格式无法识别')
    return value


def fetch_directories(entry, get):
    compatible = []
    after = None
    for _ in range(100):
        payload = get(entry['compatible_base_url'].rstrip('/') + '/models', {'after': after} if after else None)
        rows = payload.get('data')
        if not isinstance(rows, list):
            raise ValueError('兼容目录格式无法识别')
        ids = [row.get('id') for row in rows if isinstance(row, dict) and model_id(row.get('id'))]
        compatible.extend(ids)
        if not payload.get('has_more', False):
            break
        if not ids or ids[-1] == after:
            raise ValueError('兼容目录分页未推进')
        after = ids[-1]
    else:
        raise ValueError('兼容目录超过分页限制，未发布不完整目录')
    native = []
    if entry['provider'] == 'bailian':
        base = entry.get('native_http_base_url')
        if not base:
            parsed = urlsplit(entry['compatible_base_url'])
            base = f'{parsed.scheme}://{parsed.netloc}/api/v1'
        for page in range(1, 101):
            payload = get(base.rstrip('/') + '/models', {'page_no': page, 'page_size': 100, 'language': 'zh-CN'})
            container = payload.get('data', payload.get('output', payload))
            if not isinstance(container, dict) or not isinstance(container.get('models'), list):
                raise ValueError('原生目录格式无法识别')
            rows = container['models']
            native.extend(rows)
            total = container.get('total', container.get('total_count', payload.get('total')))
            if isinstance(total, int) and not isinstance(total, bool):
                if len(native) >= total:
                    break
                if not rows:
                    raise ValueError('原生目录提前结束，未发布不完整目录')
            elif len(rows) < 100:
                break
        else:
            raise ValueError('原生目录超过分页限制，未发布不完整目录')
    return merged(compatible, native)


class CatalogStore:
    def __init__(self):
        self.entries = OrderedDict()
        self.cursors = OrderedDict()

    def invalidate(self, connection_id):
        self.entries.pop(connection_id, None)

    def snapshot(self, entry):
        if entry['provider'] != 'bailian' or entry['compatible_base_url'].rstrip('/') != SNAPSHOT['base_url'].rstrip('/'):
            return None
        return {'items': merged(SNAPSHOT['compatible_ids'], SNAPSHOT['models']), 'catalog_time': SNAPSHOT['captured_at'],
                'source': 'workspace_snapshot', 'revision': 'snapshot',
                'warnings': ['历史工作空间快照，不代表当前连接的目录权限；请主动刷新。']}

    def current(self, entry):
        cached = self.entries.get(entry['id'])
        identity = tuple(entry.get(field) for field in ('provider', 'scope_id', 'compatible_base_url', 'native_http_base_url'))
        if cached and cached.get('connection_identity') == identity:
            return cached
        return self.snapshot(entry)

    def refresh(self, entry, get=None):
        if get is None:
            import httpx
            with httpx.Client(headers={'Authorization': 'Bearer ' + (entry.get('api_key') or 'local-no-key')},
                              timeout=15, follow_redirects=False) as client:
                records = fetch_directories(entry, lambda url, params: directory_get(client, url, params))
        else:
            records = fetch_directories(entry, get)
        catalog = {'items': records, 'catalog_time': datetime.now(timezone.utc).isoformat(), 'source': 'live_directory',
                   'revision': uuid.uuid4().hex, 'warnings': [],
                   'connection_identity': tuple(entry.get(field) for field in ('provider', 'scope_id', 'compatible_base_url', 'native_http_base_url'))}
        self.entries[entry['id']] = catalog
        self.entries.move_to_end(entry['id'])
        while len(self.entries) > 32:
            self.entries.popitem(last=False)
        return catalog

    def query(self, entry, request, get=None):
        if request.cursor and request.refresh:
            raise ConfigurationError('cursor', '刷新时不能使用旧游标')
        catalog = self.current(entry)
        warnings = []
        # 全目录首次打开即查询；半小时内复用同一连接目录，手动刷新仍强制 GET。
        stale = catalog is None or catalog.get('source') != 'live_directory'
        if not stale and catalog.get('catalog_time'):
            stale = (datetime.now(timezone.utc) - datetime.fromisoformat(catalog['catalog_time'])).total_seconds() > 1800
        if request.refresh or (request.role == 'all' and stale):
            try:
                catalog = self.refresh(entry, get)
            except Exception:
                warnings.append('目录刷新失败（鉴权、网络或格式）；保留已有目录，不验证推理权限。')
        if catalog is None:
            catalog = {'items': [], 'catalog_time': None, 'source': 'unavailable', 'revision': 'empty', 'warnings': ['暂无目录，请主动刷新；兼容文字/视觉可手动配置未验证型号。']}
        today = datetime.now(timezone.utc).date()
        fingerprint = (entry['id'], request.role, request.include_retiring, request.limit, catalog['revision'], today.isoformat())
        offset = 0
        if request.cursor:
            saved = self.cursors.get(request.cursor)
            if saved is None or saved[0] != fingerprint:
                raise ConfigurationError('cursor', '游标已过期或不属于此连接/查询，请从第一页刷新', status=409)
            offset = saved[1]
        def decorate(item):
            host = urlsplit(entry.get('native_http_base_url') or entry['compatible_base_url']).hostname or ''
            item['pricing'] = {'groups': item.get('prices', []), 'currency': 'CNY' if host == 'dashscope.aliyuncs.com' or '.cn-beijing.' in host else None,
                               'source': catalog['source'], 'queried_at': catalog['catalog_time'],
                               'region': host, 'reference_url': 'https://help.aliyun.com/zh/model-studio/model-pricing'}
            return item
        items = []
        for record in catalog['items']:
            if request.role == 'all':
                support = {role: item for role in ROLES if (item := candidate(record, role, entry['provider'], today)) is not None}
                exemplar = next(iter(support.values()), None)
                # 未用于本项目的图像/向量/视频等模型仍保留在完整目录，便于核对总数。
                item = {**record, 'roles': list(support), 'role_support': {role: {key: value[key] for key in ('selectable', 'adapter_implemented', 'verification', 'recommended')} for role, value in support.items()},
                        'status': exemplar['status'] if exemplar else 'active', 'recommended': False,
                        'selectable': any(value['selectable'] for value in support.values())}
            else:
                item = candidate(record, request.role, entry['provider'], today)
            if item is not None:
                items.append(decorate(item))
        if not request.include_retiring:
            items = [item for item in items if item['status'] == 'active']
        items.sort(key=lambda item: (not item['recommended'], not item['selectable'], item['id']))
        next_cursor = None
        if offset + request.limit < len(items):
            next_cursor = uuid.uuid4().hex
            self.cursors[next_cursor] = (fingerprint, offset + request.limit)
            while len(self.cursors) > 512:
                self.cursors.popitem(last=False)
        return {'schema_version': 2, 'items': items[offset:offset + request.limit], 'next_cursor': next_cursor,
                'catalog_time': catalog['catalog_time'], 'source': catalog['source'], 'total': len(items),
                'warnings': catalog['warnings'] + warnings + ['目录可见不等于推理权限、余额或模型效果验证。']}

    def validate(self, config, roles, snapshots):
        for role in roles:
            stage = config.roles()[role]
            if not stage.connection_id:
                continue
            entry = snapshots.get(stage.connection_id)
            if not entry:
                continue
            catalog = self.current({**entry, 'id': stage.connection_id})
            record = next((row for row in (catalog or {}).get('items', []) if row['id'] == stage.model), None)
            if record:
                item = candidate(record, role, stage.provider)
                field = f'config.model_config.stages.{role}.model' if role != 'avatar' else 'config.model_config.avatar.model'
                if item is None or not item['selectable']:
                    raise ConfigurationError(field, '已知目录声明的能力/协议不适配此环节，或型号已下线', 'model_not_supported')
                if role in ('text', 'vision'):
                    maximum = record.get('model_info', {}).get('max_output_tokens')
                    requested = stage.parameters.max_output_tokens
                    if maximum and requested and requested > maximum:
                        raise ConfigurationError(field.rsplit('.', 1)[0] + '.parameters.max_output_tokens', f'目录记录该型号最大输出为{maximum} tokens')

    def annotate(self, snapshots):
        for identity, entry in snapshots.items():
            catalog = self.current({**entry, 'id': identity})
            if catalog:
                entry['catalog_time'] = catalog['catalog_time']
                entry['catalog_source'] = catalog['source']

    def annotate_limits(self, snapshots, config, roles):
        for role in set(roles) & {'text', 'vision'}:
            stage = config.roles()[role]
            entry = snapshots.get(stage.connection_id)
            if not entry:
                continue
            catalog = self.current({**entry, 'id': stage.connection_id})
            record = next((row for row in (catalog or {}).get('items', []) if row['id'] == stage.model), None)
            if record and record.get('model_info'):
                entry.setdefault('model_limits', {})[stage.model] = dict(record['model_info'])


def options():
    return {'presets': ['bailian_api', 'local_existing', 'custom'], 'recommended_models': RECOMMENDED,
            'local_models': {role: {'provider': values[0], 'model': values[1], 'implemented': True} for role, values in LOCAL.items()},
            'roles': list(ROLES), 'catalog_verification': 'catalog_only'}
