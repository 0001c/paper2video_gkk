"""范围绑定的云音色目录，显式计费动作与持久化防重。"""

import base64
import json
import io
import os
import re
import threading
import uuid
import wave
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

import httpx
from pydantic import Field

from web.model_config import ConfigurationError, ConnectionID, ModelID, StrictModel, CloudVoiceReference
from bailian_speech import MODELS, SpeechClient, atomic_json, digest, native_base, wav_bytes


VoiceReference = CloudVoiceReference


class VoiceRegister(StrictModel):
    connection_id: ConnectionID
    target_model: ModelID
    voice_id: str = Field(min_length=1, max_length=200, pattern=r'^[a-zA-Z0-9_-]+$')
    label: str = Field(min_length=1, max_length=80)


class VoiceGenerate(StrictModel):
    connection_id: ConnectionID
    target_model: ModelID
    kind: Literal['clone', 'design']
    label: str = Field(min_length=1, max_length=80)
    path: str | None = Field(default=None, max_length=2000)
    prompt: str | None = Field(default=None, max_length=500)
    preview_text: str = Field(default='各位听众朋友们，大家好，欢迎收听今天的讲解。', min_length=15, max_length=60)
    consent: Literal[True]


class VoicePreview(StrictModel):
    connection_id: ConnectionID
    target_model: ModelID
    text: str = Field(default='欢迎收听今天的讲解。', min_length=1, max_length=60)


def private_oss_bucket():
    names = ('ACCESS_KEY_ID', 'ACCESS_KEY_SECRET', 'ENDPOINT', 'BUCKET', 'REGION')
    values = {name: os.environ.get('PAPER2VIDEO_VOICE_OSS_' + name, '') for name in names}
    if not all(values.values()):
        raise ConfigurationError('path', '复刻需服务端配置私有OSS；本地上传不会自动发布公网', 'media_publisher_not_configured')
    if not re.fullmatch(r'https://oss-[a-z0-9-]+\.aliyuncs\.com', values['ENDPOINT']):
        raise ConfigurationError('path', 'OSS必须使用服务端指定的阿里云HTTPS外网端点')
    try:
        import oss2
    except ImportError:
        raise ConfigurationError('path', '复刻需安装可选依赖 scripts/requirements-bailian-speech.txt') from None
    auth = oss2.AuthV4(values['ACCESS_KEY_ID'], values['ACCESS_KEY_SECRET'])
    return oss2.Bucket(auth, values['ENDPOINT'], values['BUCKET'], region=values['REGION'])


def publish_reference(filename):
    """私有 OSS 文件仅返回短时签名；凭据完全由服务端提供。"""
    bucket = private_oss_bucket()
    object_name = 'paper2video/voice-reference/' + uuid.uuid4().hex + '.wav'
    try:
        bucket.put_object_from_file(object_name, str(filename), headers={'x-oss-object-acl': 'private'})
        return bucket.sign_url('GET', object_name, 1800)
    except Exception:
        raise ConfigurationError('path', '私有OSS上传失败；未提交音色复刻') from None


class VoiceStore:
    def __init__(self, directory, uploads):
        self.directory = Path(directory)
        self.uploads = Path(uploads).resolve()
        self.lock = threading.RLock()

    def read(self):
        filename = self.directory / 'registry.json'
        return json.loads(filename.read_text(encoding='utf-8')) if filename.exists() else {'voices': {}, 'actions': {}}

    def save(self, state):
        self.directory.mkdir(parents=True, exist_ok=True)
        atomic_json(self.directory / 'registry.json', state)

    def connection(self, connections, connection_id, model=None):
        entry = connections._entry(connection_id)
        if entry['provider'] != 'bailian' or not entry.get('api_key'):
            raise ConfigurationError('connection_id', '云音色需要有效百炼连接', 'connection_expired', 409)
        native_base(entry)
        if model is not None and model not in MODELS:
            raise ConfigurationError('target_model', 'P4仅支持CosyVoice HTTP配音型号')
        return entry

    def resource(self, entry, voice_id, target_model, label, kind, status='unverified'):
        identity = {name: entry.get(name) for name in ('scope_id', 'native_http_base_url')}
        resource_id = 'voice_' + digest({**identity, 'voice_id': voice_id, 'target_model': target_model})[:24]
        return {'resource_id': resource_id, 'voice_id': voice_id, 'target_model': target_model,
                'label': label, 'kind': kind, 'provider': 'bailian', **identity, 'status': status,
                'timestamp_status': 'unknown' if kind == 'design' or status == 'unverified' else 'supported'}

    def listing(self, entry, model):
        voices = list(self.read()['voices'].values())
        if model == 'cosyvoice-v3-flash':
            system = self.resource(entry, 'longanyang', model, '龙安洋 · 中英双语', 'system', 'OK')
            if not any(voice['resource_id'] == system['resource_id'] for voice in voices):
                voices.append(system)
        return [voice for voice in voices if voice['scope_id'] == entry['scope_id'] and
                voice['native_http_base_url'] == entry['native_http_base_url'] and voice['target_model'] == model]

    def get(self, resource_id, entry, model=None):
        if model:
            records = self.listing(entry, model)
            resource = next((voice for voice in records if voice['resource_id'] == resource_id), None)
        else:
            resource = self.read()['voices'].get(resource_id)
        if not resource or resource['scope_id'] != entry['scope_id'] or resource['native_http_base_url'] != entry['native_http_base_url']:
            raise ConfigurationError('voice.resource_id', '音色不属于此连接范围或型号')
        return dict(resource)

    def register(self, payload, entry):
        if not payload.voice_id.startswith(payload.target_model + '-'):
            raise ConfigurationError('voice_id', '自定义音色ID必须匹配目标型号；系统音色从目录选择')
        kind = 'design' if payload.voice_id.startswith(payload.target_model + '-vd-') else 'clone'
        resource = self.resource(entry, payload.voice_id, payload.target_model, payload.label, kind)
        with self.lock:
            state = self.read()
            state['voices'].setdefault(resource['resource_id'], resource)
            self.save(state)
            return state['voices'][resource['resource_id']]

    def enrollment(self, entry, inputs, parameters=None, client=None):
        owned = client is None
        client = client or httpx.Client(timeout=90, follow_redirects=False)
        try:
            with client.stream('POST', native_base(entry) + '/services/audio/tts/customization',
                                   headers={'Authorization': 'Bearer ' + entry['api_key']},
                                   json={'model': 'voice-enrollment', 'input': inputs, 'parameters': parameters or {}}) as response:
                if response.status_code != 200:
                    raise ConfigurationError('voice', f'百炼音色接口拒绝请求（HTTP {response.status_code}）', 'voice_request_failed')
                body = bytearray()
                for chunk in response.iter_bytes():
                    if len(body) + len(chunk) > 16 * 1024 * 1024:
                        raise ConfigurationError('voice', '音色响应超过大小限制')
                    body.extend(chunk)
                result = json.loads(body).get('output')
            if not isinstance(result, dict):
                raise ConfigurationError('voice', '音色接口响应不完整')
            return result
        except httpx.HTTPError:
            raise ConfigurationError('voice', '云请求中断；受理状态未知，未自动重试', 'submission_unknown', 409) from None
        finally:
            if owned:
                client.close()

    def refresh(self, resource_id, entry, client=None):
        resource = self.get(resource_id, entry)
        result = self.enrollment(entry, {'action': 'query_voice', 'voice_id': resource['voice_id']}, client=client)
        if result.get('target_model') != resource['target_model']:
            raise ConfigurationError('voice.resource_id', '远程音色型号不一致')
        resource['status'] = 'OK' if result.get('status') == 'OK' else 'unavailable'
        modified = result.get('gmt_modified')
        if isinstance(modified, str) and re.fullmatch(r'[0-9T :.+Z-]{1,64}', modified):
            resource['gmt_modified'] = modified
        if result.get('voice_prompt'):
            resource.update(kind='design', timestamp_status='unknown')
        with self.lock:
            state = self.read()
            state['voices'][resource_id] = resource
            self.save(state)
        return resource

    def action(self, entry, token, inputs, operation):
        if not token or not re.fullmatch(r'[a-zA-Z0-9_-]{8,100}', token):
            raise ConfigurationError('Idempotency-Key', '每次明确云动作需8至100字符防重键')
        fingerprint = digest({'scope': entry['scope_id'], 'endpoint': entry['native_http_base_url'], 'inputs': inputs})
        action_id = digest({'scope': entry['scope_id'], 'endpoint': entry['native_http_base_url'], 'token': token})
        with self.lock:
            state = self.read()
            previous = state['actions'].get(action_id)
            if previous:
                if previous['fingerprint'] != fingerprint:
                    raise ConfigurationError('Idempotency-Key', '同键不同请求', 'idempotency_conflict', 409)
                if previous['status'] != 'succeeded':
                    raise ConfigurationError('voice', '此动作结果未知，先核对云端记录，禁止自动重提', 'submission_unknown', 409)
                return previous['result']
            state['actions'][action_id] = {'fingerprint': fingerprint, 'status': 'submission_unknown',
                                          'created_at': datetime.now(timezone.utc).isoformat()}
            self.save(state)
        result = operation()
        with self.lock:
            state = self.read()
            state['actions'][action_id].update(status='succeeded', result=result)
            self.save(state)
        return result

    def generate(self, payload, entry, token, client=None, publisher=publish_reference):
        inputs = {'action': 'create_voice', 'target_model': payload.target_model, 'prefix': 'p2v'}
        if payload.kind == 'clone':
            filename = Path(payload.path or '').resolve()
            if not filename.is_file() or not filename.is_relative_to(self.uploads):
                raise ConfigurationError('path', '请引用本服务上传的参考录音')
            if filename.stat().st_size > 10 * 1024 * 1024:
                raise ConfigurationError('path', '本项目复刻样本上限10MiB')
            try:
                with wave.open(str(filename)) as handle:
                    if handle.getsampwidth() != 2 or handle.getframerate() < 16000 or not 1 <= handle.getnchannels() <= 2 or not 5 <= handle.getnframes() / handle.getframerate() <= 60:
                        raise ValueError()
            except (wave.Error, ValueError):
                raise ConfigurationError('path', '复刻需16位WAV、至少16kHz、5至60秒；推荐10至20秒') from None
            reference_sha = digest(filename.read_bytes())
            if publisher is publish_reference:
                private_oss_bucket()
        else:
            if payload.target_model == 'cosyvoice-v2' or not payload.prompt or not payload.prompt.strip():
                raise ConfigurationError('prompt', '设计需v3/v3.5型号及有效描述')
            inputs.update(voice_prompt=payload.prompt, preview_text=payload.preview_text)
            reference_sha = None
        def submit():
            if payload.kind == 'clone':
                inputs['url'] = publisher(filename)
            result = self.enrollment(entry, inputs, {'sample_rate': 24000, 'response_format': 'wav'}, client)
            voice_id = result.get('voice_id', '')
            if not re.fullmatch(r'[a-zA-Z0-9_-]{1,200}', voice_id) or not voice_id.startswith(payload.target_model + '-'):
                raise ConfigurationError('voice', '云音色ID或目标型号无效，需人工核对', 'submission_unknown', 409)
            resource = self.resource(entry, voice_id, payload.target_model, payload.label, payload.kind)
            if reference_sha:
                resource['reference_sha'] = reference_sha
            with self.lock:
                state = self.read()
                state['voices'][resource['resource_id']] = resource
                self.save(state)
            preview = result.get('preview_audio') or {}
            if preview.get('data'):
                body = base64.b64decode(preview['data'], validate=True)
                with wave.open(io.BytesIO(body)) as handle:
                    if handle.getnframes() / handle.getframerate() > 120:
                        raise ConfigurationError('voice', '设计试听音频过长')
                (self.directory / (resource['resource_id'] + '.wav')).write_bytes(body)
                resource['sample_url'] = f"/api/cloud-voices/{resource['resource_id']}/sample"
                with self.lock:
                    state = self.read()
                    state['voices'][resource['resource_id']] = resource
                    self.save(state)
            return resource
        return self.action(entry, token, {'request': payload.model_dump(), 'reference_sha': reference_sha}, submit)

    def preview(self, resource_id, payload, entry, token, client=None):
        resource = self.get(resource_id, entry, model=payload.target_model)
        if resource['status'] != 'OK':
            raise ConfigurationError('voice', '请先显式查询音色状态')
        def submit():
            speech = SpeechClient(entry, entry['api_key'], client)
            pcm, timing = speech.synthesize(resource['target_model'], resource['voice_id'], payload.text,
                                           timestamps=resource['kind'] != 'design')
            self.directory.mkdir(parents=True, exist_ok=True)
            (self.directory / (resource_id + '.wav')).write_bytes(wav_bytes(pcm))
            resource['sample_url'] = f'/api/cloud-voices/{resource_id}/sample'
            if resource['kind'] != 'design':
                resource['timestamp_status'] = 'supported'
            with self.lock:
                state = self.read()
                state['voices'][resource_id] = resource
                self.save(state)
            return {'resource_id': resource_id, 'sample_url': f'/api/cloud-voices/{resource_id}/sample'}
        return self.action(entry, token, {'resource_id': resource_id, 'text': payload.text, 'action': 'preview'}, submit)
