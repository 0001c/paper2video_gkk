"""任务级百炼 HTTP 流式配音；不加载本地语音权重。"""

import base64
import hashlib
import io
import json
import math
import os
import re
import unicodedata
import uuid
import wave
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from web.model_connections import checked_url
from web.model_config import ConfigurationError


MODELS = frozenset(('cosyvoice-v3.5-plus', 'cosyvoice-v3.5-flash', 'cosyvoice-v3-plus',
                    'cosyvoice-v3-flash', 'cosyvoice-v2'))
SAMPLE_RATE = 24000
MAX_AUDIO_BYTES = 64 * 1024 * 1024
MAX_CHUNK_CHARACTERS = 400
GAP_SECONDS = 0.35


def digest(value):
    body = value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False, sort_keys=True).encode()
    return hashlib.sha256(body).hexdigest()


def native_base(connection):
    base = connection.get('native_http_base_url')
    if not base:
        raise ConfigurationError('connection.native_http_base_url', '请显式配置百炼原生 HTTP 地址（以 /api/v1 结尾）')
    checked_url(base, 'bailian')
    parsed = urlsplit(base)
    if parsed.path.rstrip('/') != '/api/v1' or not (parsed.hostname.endswith('.cn-beijing.maas.aliyuncs.com') or parsed.hostname == 'dashscope.aliyuncs.com'):
        raise ConfigurationError('connection.native_http_base_url', 'P4 HTTP配音仅支持北京地域 /api/v1 地址')
    return base.rstrip('/')


def plain_text(text):
    return ''.join(character.casefold() for character in unicodedata.normalize('NFKC', text)
                   if character.isalnum())


def word_identity(word):
    if type(word.get('begin_index')) is int and type(word.get('end_index')) is int:
        return word['begin_index'], word['end_index'], word['text']
    return word['begin_time'], word['text']


def validate_timing(timing, pcm, text):
    duration = len(pcm) / (2 * SAMPLE_RATE)
    words = [word for segment in timing.get('segments', []) for word in segment.get('words', [])]
    if not words or plain_text(''.join(word.get('word', '') for word in words)) != plain_text(text):
        raise ValueError('云时间戳未完整映射原稿；不会回退本地对齐')
    previous = 0.0
    for word in words:
        start, end = word['start'], word['end']
        if not all(isinstance(value, (float, int)) and not isinstance(value, bool) and math.isfinite(value) for value in (start, end)):
            raise ValueError('时间戳必须为有限数值')
        if not 0 <= previous <= start <= end <= duration + 0.001:
            raise ValueError('时间戳越界或不单调')
        previous = end


class SpeechClient:
    def __init__(self, connection, api_key, client=None, usage_directory=None, usage_role='tts'):
        self.base = native_base(connection)
        if not api_key:
            raise ConfigurationError('connection_id', '百炼语音凭据失效', 'connection_expired', 409)
        self.api_key = api_key
        self.client = client
        self.usage_directory = usage_directory
        self.usage_role = usage_role

    def synthesize(self, model, voice_id, text, rate=1.0, language='zh', timestamps=True):
        if model not in MODELS or not 0.5 <= rate <= 2 or not text.strip() or len(text) > MAX_CHUNK_CHARACTERS:
            raise ValueError('云配音型号、语速或分块文本不合法')
        payload = {'model': model, 'input': {'text': text, 'voice': voice_id, 'format': 'pcm',
                   'sample_rate': SAMPLE_RATE, 'rate': rate, 'word_timestamp_enabled': timestamps}}
        if language in ('zh', 'en'):
            payload['input']['language_hints'] = [language]
        client = self.client or httpx.Client(timeout=httpx.Timeout(180, connect=20), follow_redirects=False)
        from web import model_costs
        ticket = model_costs.begin(self.usage_role, model, self.base, self.usage_directory)
        usage = {}
        try:
            with client.stream('POST', self.base + '/services/audio/tts/SpeechSynthesizer',
                               headers={'Authorization': 'Bearer ' + self.api_key, 'X-DashScope-SSE': 'enable'}, json=payload) as response:
                if response.status_code != 200:
                    model_costs.finish(ticket, state='rejected')
                    raise ValueError('百炼配音请求被拒绝（HTTP {}）；未自动重试'.format(response.status_code))
                pcm, sentences, finished = bytearray(), {}, False
                pending = []
                total = 0
                for line in response.iter_lines():
                    total += len(line)
                    if total > MAX_AUDIO_BYTES * 2 or len(line) > 4 * 1024 * 1024:
                        raise ValueError('百炼配音响应超过大小限制')
                    if line.startswith('data:'):
                        pending.append(line[5:].lstrip())
                    if line or not pending:
                        continue
                    data = '\n'.join(pending)
                    pending = []
                    if data == '[DONE]':
                        continue
                    event = json.loads(data)
                    if event.get('code'):
                        raise ValueError('百炼配音返回错误；未自动重试')
                    output = event.get('output') or {}
                    usage.update(event.get('usage') or {})
                    encoded = (output.get('audio') or {}).get('data')
                    if encoded:
                        pcm.extend(base64.b64decode(encoded, validate=True))
                        if len(pcm) > MAX_AUDIO_BYTES:
                            raise ValueError('配音音频超过大小限制')
                    sentence = output.get('sentence') or {}
                    if sentence.get('words'):
                        words = sentences.setdefault(sentence['index'], {})
                        for word in sentence['words']:
                            identity = word_identity(word)
                            words[identity] = word
                    finished = finished or output.get('finish_reason') == 'stop'
                if pending or not finished or not pcm or len(pcm) % 2:
                    raise ValueError('云配音流不完整；受理状态可能未知，禁止自动重试')
                timing = {'segments': [{'words': [{'word': word['text'], 'start': word['begin_time'] / 1000,
                           'end': word['end_time'] / 1000} for word in sorted(sentences[index].values(), key=lambda value: (value['begin_time'], value['end_time'])) if plain_text(word['text'])]} for index in sorted(sentences)]}
                model_costs.finish(ticket, usage if 'characters' in usage else {'characters': len(text)}, source='provider' if 'characters' in usage else 'submitted_text_estimate')
                if timestamps:
                    validate_timing(timing, pcm, text)
                return bytes(pcm), timing
        except httpx.HTTPError:
            raise ValueError('云配音连接中断；可能仍在计费，未自动重试') from None
        finally:
            if self.client is None:
                client.close()


def atomic_json(filename, value):
    target = Path(filename)
    temporary = target.with_name(target.name + '.' + uuid.uuid4().hex + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(target)


def wav_bytes(pcm):
    stream = io.BytesIO()
    with wave.open(stream, 'wb') as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(SAMPLE_RATE)
        handle.writeframes(pcm)
    return stream.getvalue()


def read_pair(audio_path, text=None):
    target = Path(audio_path)
    timing = json.loads(target.with_suffix('.timing.json').read_text(encoding='utf-8'))
    body = target.read_bytes()
    with wave.open(io.BytesIO(body), 'rb') as handle:
        if (handle.getframerate(), handle.getnchannels(), handle.getsampwidth()) != (SAMPLE_RATE, 1, 2):
            raise ValueError('云配音缓存采样格式无效')
        pcm = handle.readframes(handle.getnframes())
    if timing.get('schema_version') != 1 or timing.get('source') != 'tts_timestamp' or timing.get('audio_sha256') != digest(body):
        raise ValueError('云音频/时间戳配对无效')
    original = timing.get('text', '')
    if not isinstance(original, str) or timing.get('script_sha256') != digest(original):
        raise ValueError('云时间戳原稿指纹无效')
    if text is not None and (plain_text(original) != plain_text(text) or timing.get('script_sha256') != digest(text)):
        raise ValueError('云配音缓存原稿已变化')
    if timing.get('sample_count') != len(pcm) // 2 or timing.get('sample_rate') != SAMPLE_RATE:
        raise ValueError('云配音缓存采样数量不一致')
    validate_timing(timing, pcm, original)
    return pcm, timing


def write_pair(filename, pcm, timing, text):
    validate_timing(timing, pcm, text)
    body = wav_bytes(pcm)
    timing = {**timing, 'schema_version': 1, 'source': 'tts_timestamp', 'audio_sha256': digest(body),
              'script_sha256': digest(text), 'sample_rate': SAMPLE_RATE, 'sample_count': len(pcm) // 2,
              'text': text, 'time_unit': 'seconds', 'time_origin': 'page'}
    timing['normalization_map'] = [{'original_index': original_index, 'normalized': plain_text(character)}
                                   for original_index, character in enumerate(text) if plain_text(character)]
    target = Path(filename)
    temporary = target.with_name(target.name + '.' + uuid.uuid4().hex + '.tmp')
    temporary.write_bytes(body)
    temporary.replace(target)
    atomic_json(target.with_suffix('.timing.json'), timing)
    return timing


def chunks(text):
    remaining = text
    while remaining:
        boundary = min(MAX_CHUNK_CHARACTERS, len(remaining))
        if len(remaining) > MAX_CHUNK_CHARACTERS:
            candidates = [match.end() for match in re.finditer(r'[。！？!?；;\n\s]', remaining[:MAX_CHUNK_CHARACTERS]) if match.end() >= MAX_CHUNK_CHARACTERS // 2]
            boundary = candidates[-1] if candidates else MAX_CHUNK_CHARACTERS
        yield remaining[:boundary]
        remaining = remaining[boundary:]


def tts_pages(parsed, directory, snapshot, resume=True, client=None):
    stage = snapshot['model_config']['stages']['tts']
    connection = snapshot['model_connections'][stage['connection_id']]
    voice = snapshot.get('voice_resource') or {}
    if voice.get('target_model') != stage['model'] or voice.get('scope_id') != connection['scope_id'] or voice.get('native_http_base_url') != connection.get('native_http_base_url'):
        raise ValueError('云音色与任务模型/连接范围不一致')
    if voice.get('timestamp_status') != 'supported' or voice.get('status') != 'OK':
        raise ValueError('此云音色尚未验证可用或不能提供时间戳')
    speech = client or SpeechClient(connection, os.environ.get('PAPER2VIDEO_ROLE_TTS_API_KEY'))
    target = Path(directory)
    target.mkdir(parents=True, exist_ok=True)
    manifest_path = target / 'manifest.json'
    manifest = json.loads(manifest_path.read_text(encoding='utf-8')) if resume and manifest_path.exists() else {}
    identity = {'mode': 'api', 'provider': 'bailian', 'model': stage['model'], 'scope': connection['scope_id'],
                'endpoint': connection['native_http_base_url'],
                'voice': {name: voice.get(name) for name in ('voice_id', 'target_model', 'language', 'reference_sha', 'gmt_modified')},
                'parameters': stage['parameters'], 'timing_version': 1,
                'chunk_strategy': {'max_characters': MAX_CHUNK_CHARACTERS, 'gap_seconds': GAP_SECONDS}}
    for page_index, lines in enumerate(parsed):
        text = '\n\n\n'.join(prompt for prompt, cursor in lines)
        if not text.strip():
            continue
        filename = target / f'{page_index}.wav'
        key = digest({'identity': identity, 'text': text})
        saved = manifest.get(str(page_index), {})
        if resume and saved.get('key') == key:
            try:
                read_pair(filename, text)
                if saved.get('wav_sha') != digest(filename.read_bytes()) or saved.get('timing_sha') != digest(filename.with_suffix('.timing.json').read_bytes()):
                    raise ValueError('配对缓存manifest不一致')
                print(f'[speech_gen] 第 {page_index + 1} 页云配音配对缓存复用', flush=True)
                import progress_report
                progress_report.report('tts', stage=2, done=page_index + 1, total=len(parsed), page=page_index)
                continue
            except (ValueError, OSError, KeyError):
                pass
        part_dir = target / '.cloud_parts' / str(page_index)
        part_dir.mkdir(parents=True, exist_ok=True)
        page_pcm, segments, parts = bytearray(), [], []
        for part_index, chunk in enumerate(chunks(text)):
            part = part_dir / (digest({'identity': identity, 'text': chunk}) + '.wav')
            try:
                if not resume:
                    raise FileNotFoundError()
                pcm, timing = read_pair(part, chunk)
            except (ValueError, OSError, KeyError):
                stamp = part.with_suffix('.submission.json')
                if stamp.exists():
                    raise ValueError('该配音分块受理状态未知；需人工核对，禁止自动重提')
                atomic_json(stamp, {'state': 'submission_unknown', 'key': part.stem,
                                   'submitted_at': datetime.now(timezone.utc).isoformat(),
                                   'model': stage['model'], 'scope_id': connection['scope_id'], 'text_sha256': digest(chunk)})
                pcm, timing = speech.synthesize(stage['model'], voice['voice_id'], chunk,
                                               stage['parameters'].get('speech_rate', 1.0), snapshot.get('voice_resource', {}).get('language', 'zh'))
                timing = write_pair(part, pcm, timing, chunk)
                stamp.unlink()
            if part_index:
                page_pcm.extend(bytes(int(SAMPLE_RATE * GAP_SECONDS) * 2))
            offset = len(page_pcm) / (2 * SAMPLE_RATE)
            parts.append({'index': part_index, 'start_sample': len(page_pcm) // 2, 'sample_count': len(pcm) // 2,
                          'text_sha256': digest(chunk)})
            segments.extend({'words': [{**word, 'start': word['start'] + offset, 'end': word['end'] + offset}
                                       for word in segment['words']]} for segment in timing['segments'])
            page_pcm.extend(pcm)
        timing = write_pair(filename, bytes(page_pcm), {'segments': segments, 'chunks': parts}, text)
        manifest[str(page_index)] = {'key': key, **identity, 'wav_sha': timing['audio_sha256'],
                                    'timing_sha': digest(filename.with_suffix('.timing.json').read_bytes()),
                                    'seconds': timing['sample_count'] / SAMPLE_RATE}
        atomic_json(manifest_path, manifest)
        print(f'[speech_gen] 第 {page_index + 1} 页百炼配音完成', flush=True)
        import progress_report
        progress_report.report('tts', stage=2, done=page_index + 1, total=len(parsed), page=page_index)
