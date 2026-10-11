"""离线配置单元检查；不启动服务、不调用HTTP/模型、不加载GPU。"""

import copy
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pydantic import ValidationError


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'Paper2Video-main/src'))

from web.model_config import ConfigurationError, PipelineModelConfig, derived_policy, ensure_implemented, legacy_config, required_roles
from web.model_connections import ConnectionCreate, ConnectionStore, ConnectionUpdate, checked_url
from model_runtime import apply_model_snapshot


def expect_error(operation, error_type):
    try:
        operation()
    except error_type:
        return
    raise AssertionError('应拒绝无效配置')


def main():
    with patch.dict(os.environ, {}, clear=True):
        store = ConnectionStore()
        connection = store.create(ConnectionCreate(label='离线连接', provider='openai_compatible',
                                  compatible_base_url='http://localhost:9000/v1', api_key='unit-key-first'))
        connection_id = connection['id']
        assert 'api_key' not in connection and 'unit-key-first' not in json.dumps(connection)
        config = legacy_config({'text_model': 'unit-text', 'vision_model': 'unit-vision', 'generate_talking': False}, connection_id)
        config.preset = 'local_existing'
        config.stages.text.mode = config.stages.vision.mode = 'local'
        config = PipelineModelConfig.model_validate(config.public_dict())
        ensure_implemented(config, ['text', 'vision', 'tts', 'grounding'])
        assert derived_policy(config)['max_segment_seconds_exclusive'] is None
        config.avatar.enabled = True
        assert derived_policy(config)['max_segment_seconds_exclusive'] == 20
        ensure_implemented(config, ['avatar'])
        config.avatar.enabled = False
        assert required_roles(config, {'stages': ['2'], 'reuse_script': True, 'require_reuse_script': True}) == ['grounding', 'tts']
        roles = ['text', 'vision']
        snapshots, keys = store.freeze(config, roles)
        store.update(connection_id, ConnectionUpdate(api_key='unit-key-second'))
        frozen, frozen_keys = store.freeze(config, roles, snapshots, keys)
        assert frozen_keys[connection_id] == 'unit-key-first'
        assert frozen == snapshots
        assert store.freeze(config, roles)[1][connection_id] == 'unit-key-second'
        invalid = config.public_dict()
        invalid['stages']['tts']['parameters']['word_timestamp_enabled'] = False
        expect_error(lambda: PipelineModelConfig.model_validate(invalid), ValidationError)
        invalid = config.public_dict()
        invalid['avatar']['enabled'] = 'false'
        expect_error(lambda: PipelineModelConfig.model_validate(invalid), ValidationError)
        invalid = config.public_dict()
        invalid['stages']['tts'].update(mode='api', provider='bailian', connection_id=connection_id, model='cosyvoice-v3.5-plus')
        candidate = PipelineModelConfig.model_validate(invalid)
        expect_error(lambda: ensure_implemented(candidate, ['tts']), ConfigurationError)
        invalid = config.public_dict()
        invalid['stages']['vision']['provider'] = 'bailian'
        invalid['stages']['vision']['mode'] = 'api'
        expect_error(lambda: store.freeze(PipelineModelConfig.model_validate(invalid), roles), ConfigurationError)
        for address in ('http://169.254.169.254/v1', 'http://0.0.0.0/v1', 'http://user:password@localhost/v1', 'http://localhost/v1?key=value'):
            expect_error(lambda: checked_url(address, 'openai_compatible'), ValueError)
        # 官方云端无需额外环境白名单；不扩大为 deepseek.com 的任意子域。
        for address, normalized in (('https://api.deepseek.com', 'https://api.deepseek.com'),
                                    ('https://API.DEEPSEEK.COM/v1/', 'https://api.deepseek.com/v1'),
                                    ('https://api.deepseek.com:443/v1', 'https://api.deepseek.com:443/v1')):
            assert checked_url(address, 'openai_compatible') == normalized
        cloud = store.create(ConnectionCreate(label='DeepSeek 官方', provider='openai_compatible',
                             compatible_base_url='https://api.deepseek.com/v1', api_key='offline-deepseek-key'))
        assert cloud['key_present'] and cloud['compatible_base_url'] == 'https://api.deepseek.com/v1'
        assert 'offline-deepseek-key' not in json.dumps(cloud)
        store.delete(cloud['id'])
        for address in ('http://api.deepseek.com', 'https://api.deepseek.com:8443/v1',
                        'https://api.deepseek.com.evil.invalid/v1', 'https://evilapi.deepseek.com/v1',
                        'https://api.deepseek.com@localhost/v1', 'https://user:key@api.deepseek.com/v1',
                        'https://api.deepseek.com/v1?key=secret', 'https://api.deepseek.com/v1#secret',
                        'https://api.deepseek.com./v1', 'https://unknown-cloud.invalid/v1'):
            expect_error(lambda: checked_url(address, 'openai_compatible'), ValueError)
        with patch.dict(os.environ, {'PAPER2VIDEO_MODEL_HOSTS': 'custom-cloud.invalid'}):
            assert checked_url('https://custom-cloud.invalid/v1', 'openai_compatible') == 'https://custom-cloud.invalid/v1'
            expect_error(lambda: checked_url('https://custom-cloud.invalid.evil.invalid/v1', 'openai_compatible'), ValueError)
        expect_error(lambda: checked_url('http://dashscope.aliyuncs.com/v1', 'bailian'), ValueError)
        workspace = 'llm-unit.cn-beijing.maas.aliyuncs.com'
        assert checked_url(f'https://{workspace}/api/v1', 'bailian') == f'https://{workspace}/api/v1'
        assert checked_url(f'wss://{workspace}/api-ws/v1/inference', 'bailian', ('wss',)).startswith('wss://')
        speech_connection = store.create(ConnectionCreate(label='完整配音连接', provider='bailian',
            compatible_base_url=f'https://{workspace}/compatible-mode/v1', native_http_base_url=f'https://{workspace}/api/v1',
            websocket_url=f'wss://{workspace}/api-ws/v1/inference', api_key='offline-key'))
        assert speech_connection['native_http_base_url'].endswith('/api/v1')
        assert speech_connection['websocket_url'].endswith('/api-ws/v1/inference')
        store.delete(speech_connection['id'])
        for address in ('https://maas.qianwenapi.com/api/v1', 'https://evilmaas.aliyuncs.com/api/v1', 'https://dashscope.aliyuncs.com.evil.invalid/api/v1'):
            expect_error(lambda: checked_url(address, 'bailian'), ValueError)
        store.delete(connection_id)
        expect_error(lambda: store.freeze(config, roles), ConfigurationError)
        replacement = store.create(ConnectionCreate(label='重绑', provider='openai_compatible', scope_id=connection['scope_id'],
                                   compatible_base_url='http://localhost:9000/v1', api_key='unit-key-rebound'))
        assert replacement['scope_id'] == connection['scope_id']
        with tempfile.TemporaryDirectory(prefix='model-config-', dir=ROOT) as directory:
            path = Path(directory, 'model_config.json')
            snapshot = {'model_config': config.public_dict(), 'model_config_origin': 'v2', 'model_connections': snapshots, 'derived_policy': derived_policy(config)}
            path.write_text(json.dumps(snapshot), encoding='utf-8')
            args = SimpleNamespace(model_config=str(path), stage='["1", "2"]', review_before_render=True,
                                   ref_audio=None, ref_text=None, generate_talking=True)
            expect_error(lambda: apply_model_snapshot(args), ConfigurationError)
            os.environ.update(PAPER2VIDEO_ROLE_TEXT_API_KEY='unit-key-first', PAPER2VIDEO_ROLE_VISION_API_KEY='unit-key-first',
                              OPENAI_API_KEY='wrong-global-key', OPENAI_BASE_URL='http://wrong.example/v1')
            apply_model_snapshot(args)
            assert 'OPENAI_API_KEY' not in os.environ
            assert 'OPENAI_BASE_URL' not in os.environ
            assert args.model_name_t == 'unit-text' and args.generate_talking is False
            assert 'unit-key-first' not in path.read_text(encoding='utf-8')
            poisoned = copy.deepcopy(snapshot)
            poisoned['model_connections'][connection_id]['api_key'] = 'must-not-be-written'
            path.write_text(json.dumps(poisoned), encoding='utf-8')
            expect_error(lambda: apply_model_snapshot(args), ConfigurationError)
        from web import app as web
        web._MODEL_CONNECTIONS = store
        from fastapi.testclient import TestClient
        client = TestClient(web.app, client=('127.0.0.1', 50000))
        payload = {'label': 'DeepSeek 官方离线校验', 'provider': 'openai_compatible',
                   'compatible_base_url': 'https://api.deepseek.com/v1', 'api_key': 'offline-api-secret'}
        created = client.post('/api/model-connections', json=payload)
        assert created.status_code == 201, created.text
        record = created.json()
        assert record['key_present'] and 'offline-api-secret' not in created.text
        store.delete(record['id'])
        rejected = client.post('/api/model-connections', json={**payload, 'compatible_base_url': 'https://api.deepseek.com.evil.invalid/v1'})
        assert rejected.status_code == 422
        assert rejected.json()['detail']['problems'][0]['field'] == 'compatible_base_url'
        config.stages.text.connection_id = config.stages.vision.connection_id = replacement['id']
        incoming = config.public_dict()
        # 已保存的预置、设计样本必须变成参考音频引用；选择和验证不能再次调用生成接口。
        with tempfile.TemporaryDirectory(prefix='saved-voices-', dir=ROOT) as directory, patch.object(web.mimo_voice, 'CACHE_DIR', directory), patch.object(web.mimo_voice, 'generate', side_effect=AssertionError('不能生成')):
            voice = next(item for item in web.mimo_voice.PRESET_VOICES if item['lang'] == 'zh')
            preset_key = web.mimo_voice.cache_key('preset', voice['id'], None, 'zh')
            for key, metadata in ((preset_key, {'kind': 'preset', 'voice': voice['id'], 'lang': 'zh'}), ('saved_design', {'kind': 'design', 'prompt': '稳重', 'lang': 'zh'})):
                Path(directory, key + '.wav').write_bytes(b'offline-reference-audio')
                Path(directory, key + '.txt').write_text('参考原文', encoding='utf-8')
                Path(directory, key + '.json').write_text(json.dumps({'key': key, **metadata}), encoding='utf-8')
            candidates = web.mimo_voice.voices_for('zh')
            saved = [item for item in candidates['presets'] + candidates['custom'] if item.get('sample_url')]
            assert len(saved) == 2 and all(item['spec']['kind'] == 'upload' for item in saved)
            assert not web.mimo_voice.voices_for('en')['custom']
            for item in saved:
                candidate = {'model_config': incoming, 'stages': ['1', '2'], 'review_before_render': True, 'voice': item['spec']}
                web._configure_job_models(candidate, {'model_config': incoming})
                assert candidate['ref_audio'] == item['spec']['path'] and candidate['ref_text'] == '参考原文'
                assert web.mimo_voice.resolve(item['spec'], 'zh')[:2] == (item['spec']['path'], '参考原文')
            Path(directory, preset_key + '.wav').unlink()
            assert not next(item for item in web.mimo_voice.voices_for('zh')['presets'] if item.get('source_spec', item['spec']).get('voice') == voice['id']).get('sample_url')
        job_config = {'model_config': incoming, 'stages': ['1', '2'], 'review_before_render': True, 'voice': {'kind': 'default'}}
        credentials = web._configure_job_models(job_config, {'config': {'model_config': incoming}})
        assert credentials[replacement['id']] == 'unit-key-rebound'
        assert job_config['model_config_origin'] == 'v2' and job_config['text_model'] == 'unit-text'
        environment = web._job_model_environment(job_config, {'connections': credentials, 'api_key': 'old-global-key', 'api_base': 'http://wrong.example/v1'})
        assert 'OPENAI_API_KEY' not in environment and 'OPENAI_BASE_URL' not in environment
        assert environment['PAPER2VIDEO_ROLE_TEXT_API_KEY'] == 'unit-key-rebound'
        invalid = copy.deepcopy(incoming)
        invalid['stages']['tts']['provider'] = 'gui_owl'
        try:
            web._parse_model_config(invalid)
        except web.HTTPException as exc:
            assert exc.detail['problems'][0]['field'] == 'config.model_config.stages.tts.provider'
        else:
            raise AssertionError('提供方错误未定位到字段')
        expect_error(lambda: web._configure_job_models(copy.deepcopy(job_config), {'model_config': incoming, 'api_key': 'conflicting-key'}), ConfigurationError)
        restored = copy.deepcopy(job_config)
        original_job = {'id': 'offline-job', 'config': job_config}
        web._JOB_KEYS['offline-job'] = {'connections': credentials}
        assert web._configure_job_models(restored, source=original_job) == credentials
        old_config = {'text_model': 'old-text', 'vision_model': 'old-vision', 'generate_talking': False}
        web._configure_job_models(old_config)
        assert old_config['model_config_origin'] == 'legacy' and old_config['model_config']['stages']['text']['model'] == 'old-text'
        web.ReviewRunRequest.model_validate({'version': '0' * 64, 'pages': ['原稿 | no'], 'action': 'render', 'model_config': incoming})
        with tempfile.TemporaryDirectory(prefix='model-job-', dir=ROOT) as directory, patch.object(web, 'JOBS_ROOT', directory):
            job_dir = Path(directory, 'job_unit')
            job_dir.mkdir()
            saved_config = {**job_config, 'slide_lang': 'en', 'script_lang': 'zh'}
            saved_job = {'id': 'job_unit', 'config': saved_config, 'paper_latex_root': str(job_dir / 'latex_proj')}
            web._save_job(saved_job)
            persisted = (job_dir / 'model_config.json').read_text(encoding='utf-8')
            assert 'unit-key-rebound' not in persisted and 'unit-key-rebound' not in (job_dir / 'job.json').read_text(encoding='utf-8')
            assert '--model-config' in web._build_argv(saved_job)
        from starlette.requests import Request
        async def check_body(body, origin=None):
            async def receive():
                return {'type': 'http.request', 'body': body, 'more_body': False}
            headers = [(b'host', b'localhost:8848')]
            if origin:
                headers.append((b'origin', origin.encode()))
            request = Request({'type': 'http', 'method': 'POST', 'path': '/api/model-connections', 'scheme': 'http',
                               'server': ('localhost', 8848), 'client': ('127.0.0.1', 1), 'headers': headers, 'query_string': b''}, receive)
            async def next_call(request):
                assert await request.body() == body
                return web.JSONResponse({'ok': True})
            return await web.review_body_limit(request, next_call)
        assert asyncio.run(check_body(b'{}')).status_code == 200
        assert asyncio.run(check_body(b'x' * 16385)).status_code == 413
        assert asyncio.run(check_body(b'{}', 'http://attacker.invalid')).status_code == 403
        options = asyncio.run(web.api_options())
        assert options['model_configuration']['schema_version'] == 2
    print('配置、连接隔离、旧配置迁移及CLI快照离线单元检查通过；未调用模型/HTTP。')


if __name__ == '__main__':
    main()
