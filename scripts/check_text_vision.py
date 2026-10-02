"""P3离线回归：角色路由、SDK请求体、结构化续写、课程与审核重跑。"""

import asyncio
import copy
import json
import os
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'Paper2Video-main' / 'src'), str(ROOT / 'scripts')]

from web.model_catalog import CatalogStore, SNAPSHOT
from web.model_config import ConfigurationError, PipelineModelConfig, ensure_implemented, legacy_config
from web.model_connections import ConnectionCreate, ConnectionStore
from model_runtime import apply_model_snapshot, role_settings

with patch.dict(os.environ, {'PAPER2VIDEO_MODEL_CONFIG_ORIGIN': 'v2'}):
    import course
    import paper_struct
    import wei_utils
    from web import app as web


class TextVisionChecks(unittest.TestCase):
    def setUp(self):
        environment = {name: value for name, value in os.environ.items() if not name.startswith(('OPENAI_', 'DEEPSEEK_', 'PAPER2VIDEO_ROLE_'))}
        self.environment = patch.dict(os.environ, {**environment, 'PAPER2VIDEO_MODEL_CONFIG_ORIGIN': 'v2',
            'PAPER2VIDEO_ROLE_TEXT_API_KEY': 'offline-text-key', 'PAPER2VIDEO_ROLE_VISION_API_KEY': 'offline-vision-key',
            'OPENAI_API_KEY': 'forbidden-global-key', 'OPENAI_BASE_URL': 'http://wrong.invalid/v1',
            'PAPER2VIDEO_MAX_TOKENS': '65536'}, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.config = legacy_config({'text_model': 'same-model', 'vision_model': 'same-model', 'generate_talking': False}, 'text')
        self.config.stages.text.parameters.max_output_tokens = 2048
        self.config.stages.text.parameters.temperature = 0.2
        self.config.stages.vision.connection_id = 'vision'
        self.config.stages.vision.mode = 'local'
        self.config.stages.vision.parameters.max_output_tokens = 4096
        self.config.stages.vision.parameters.temperature = 0.8
        self.snapshot = {'model_config': self.config.public_dict(), 'model_config_origin': 'v2', 'derived_policy': {},
                         'model_connections': {
                             'text': {'provider': 'openai_compatible', 'scope_id': 'text-scope', 'compatible_base_url': 'http://localhost:9001/v1'},
                             'vision': {'provider': 'openai_compatible', 'scope_id': 'vision-scope', 'compatible_base_url': 'http://localhost:9002/v1'}}}

    def test_independent_connections_same_model_and_no_global_mutation(self):
        import camel.agents
        import camel.models
        ensure_implemented(self.config, ['text', 'vision'])
        before = dict(os.environ)
        text = wei_utils.get_agent_config('ignored', 'text', self.snapshot)
        vision = wei_utils.get_agent_config('ignored', 'vision', self.snapshot)
        with patch('camel.models.ModelFactory.create', side_effect=lambda **kwargs: SimpleNamespace(**kwargs)), \
             patch('camel.agents.ChatAgent', side_effect=lambda **kwargs: kwargs['model']):
            with ThreadPoolExecutor(max_workers=4) as pool:
                clients = list(pool.map(wei_utils.create_agent, [text, vision] * 8))
        self.assertEqual({item.url for item in clients}, {'http://localhost:9001/v1', 'http://localhost:9002/v1'})
        for index, client in enumerate(clients):
            expected = 'vision' if index % 2 else 'text'
            self.assertEqual(client.api_key, f'offline-{expected}-key')
            self.assertEqual(client.model_config_dict['max_tokens'], 4096 if index % 2 else 2048)
            self.assertEqual(client.model_config_dict['temperature'], 0.8 if index % 2 else 0.2)
        self.assertEqual(dict(os.environ), before)
        self.assertNotIn('offline-text-key', str(text))
        self.assertNotIn('offline-vision-key', str(vision))

    def test_missing_role_key_snapshot_and_provider_fail_closed(self):
        os.environ.pop('PAPER2VIDEO_ROLE_VISION_API_KEY')
        with self.assertRaises(ConfigurationError):
            role_settings(self.snapshot, 'vision')
        with self.assertRaises(ConfigurationError):
            wei_utils.get_agent_config('same-model', 'text')
        with self.assertRaises(ValueError):
            wei_utils.create_agent({'api_key_env': 'PAPER2VIDEO_ROLE_VISION_API_KEY'})
        broken = copy.deepcopy(self.snapshot)
        broken['model_connections']['text']['provider'] = 'bailian'
        with self.assertRaises(ConfigurationError):
            role_settings(broken, 'text')

    def test_snapshot_clears_legacy_aliases_and_only_requires_active_roles(self):
        os.environ['OPENAI_COMPATIBILITY_API_KEY'] = 'forbidden-compatible-key'
        os.environ['OPENAI_PROJECT_ID'] = 'forbidden-global-project'
        os.environ.pop('PAPER2VIDEO_ROLE_TEXT_API_KEY')
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            filename = Path(directory, 'snapshot.json')
            filename.write_text(json.dumps(self.snapshot), encoding='utf-8')
            args = SimpleNamespace(model_config=str(filename), stage='["2"]', review_before_render=True,
                                   reuse_script=False, ref_audio=None, ref_text=None, generate_talking=False)
            apply_model_snapshot(args)
        self.assertEqual(args.required_model_roles, ['vision'])
        self.assertFalse(any(name.startswith(('OPENAI_', 'DEEPSEEK_')) for name in os.environ))
        self.assertEqual(role_settings(self.snapshot, 'vision')['url'], 'http://localhost:9002/v1')

    def test_bailian_limits_are_catalog_scoped_and_frozen(self):
        config = copy.deepcopy(self.config)
        config.stages.vision.mode = 'api'
        config.stages.vision.provider = 'bailian'
        config.stages.vision.model = 'qwen3-vl-plus'
        entry = {'id': 'vision', 'provider': 'bailian', 'compatible_base_url': SNAPSHOT['base_url']}
        store = CatalogStore()
        snapshots = {'vision': entry}
        store.validate(config, ['vision'], snapshots)
        store.annotate_limits(snapshots, config, ['vision'])
        self.assertEqual(entry['model_limits']['qwen3-vl-plus']['max_output_tokens'], 32768)
        self.assertEqual(entry['model_limits']['qwen3-vl-plus']['context_window'], 262144)
        snapshot = {**self.snapshot, 'model_config': config.public_dict(), 'model_connections': snapshots}
        settings = role_settings(snapshot, 'vision')
        self.assertEqual(settings['model_config']['extra_body'], {'enable_thinking': False})
        config.stages.vision.parameters.max_output_tokens = 32769
        with self.assertRaises(ConfigurationError) as caught:
            store.validate(config, ['vision'], snapshots)
        self.assertEqual(caught.exception.field, 'config.model_config.stages.vision.parameters.max_output_tokens')
        with self.assertRaises(ConfigurationError):
            role_settings({**snapshot, 'model_config': config.public_dict()}, 'vision')
        other = {**entry, 'compatible_base_url': 'https://dashscope.aliyuncs.com/compatible-mode/v1'}
        other.pop('model_limits')
        self.assertIsNone(store.current(other))

    def test_pure_backend_validation_env_and_persisted_job_have_no_keys(self):
        store = ConnectionStore()
        text = store.create(ConnectionCreate(label='文字', provider='openai_compatible', compatible_base_url='http://localhost:9001/v1', api_key='offline-text-key'))
        vision = store.create(ConnectionCreate(label='视觉本地', provider='openai_compatible', compatible_base_url='http://localhost:9002/v1'))
        config = copy.deepcopy(self.config)
        config.stages.text.connection_id = text['id']
        config.stages.vision.connection_id = vision['id']
        job_config = {'stages': ['1', '2'], 'review_before_render': True, 'voice': {'kind': 'default'}, 'slide_lang': 'en', 'script_lang': 'zh'}
        with patch.object(web, '_MODEL_CONNECTIONS', store), patch.object(web, '_MODEL_CATALOG', CatalogStore()):
            keys = web._configure_job_models(job_config, {'model_config': config.public_dict()})
        env = web._job_model_environment(job_config, {'connections': keys, 'api_key': 'forbidden-global-key'})
        self.assertEqual(env['PAPER2VIDEO_ROLE_TEXT_API_KEY'], 'offline-text-key')
        self.assertEqual(env['PAPER2VIDEO_ROLE_VISION_API_KEY'], 'local-no-key')
        self.assertFalse(any(name.startswith(('OPENAI_', 'DEEPSEEK_')) for name in env))
        self.assertNotIn('model_api_base', job_config)
        self.assertEqual(job_config['effective_model_mode'], 'mixed')
        with tempfile.TemporaryDirectory(dir=ROOT) as directory, patch.object(web, 'JOBS_ROOT', directory):
            job_dir = Path(directory, 'job_p3')
            job_dir.mkdir()
            job = {'id': 'job_p3', 'config': job_config, 'paper_latex_root': str(job_dir / 'latex_proj')}
            web._save_job(job)
            for name in ('job.json', 'model_config.json'):
                persisted = (job_dir / name).read_text(encoding='utf-8')
                self.assertNotIn('offline-text-key', persisted)
                self.assertNotIn('forbidden-global-key', persisted)
            argv = web._build_argv(job)
            self.assertIn('--model-config', argv)
            self.assertNotIn('offline-text-key', ' '.join(argv))

    def test_struct_request_and_continuation_use_text_config(self):
        response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='continued', reasoning_content='hidden'), finish_reason='stop')], usage=None)
        with patch('openai.OpenAI') as sdk:
            sdk.return_value.__enter__.return_value.chat.completions.create.return_value = response
            content, info = paper_struct.call_llm('system', 'user', model_snapshot=self.snapshot)
            self.assertEqual(content, 'continued')
            self.assertEqual(info['usage']['reasoning_chars'], 6)
            self.assertEqual(sdk.call_args.kwargs['api_key'], 'offline-text-key')
            self.assertEqual(sdk.call_args.kwargs['base_url'], 'http://localhost:9001/v1')
            request = sdk.return_value.__enter__.return_value.chat.completions.create.call_args.kwargs
            self.assertEqual(request['model'], 'same-model')
            self.assertEqual(request['max_tokens'], 2048)
            self.assertEqual(request['temperature'], 0.2)
            with self.assertRaises(ValueError):
                paper_struct.call_llm('system', 'user', max_tokens=100, model_snapshot=self.snapshot)
        with patch.object(paper_struct, 'call_llm', return_value=('\\end{document}', {'usage': {}, 'elapsed': 0, 'finish_reason': 'stop'})) as call:
            paper_struct._continue_tex('\\begin{document}', {'usage': {}, 'elapsed': 0}, None, None, model_snapshot=self.snapshot)
            self.assertIs(call.call_args.kwargs['model_snapshot'], self.snapshot)

    def test_struct_subprocess_receives_snapshot_not_keys(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            source = Path(directory, 'source.pdf')
            source.touch()
            latex = Path(directory, 'latex_proj')
            job = {'id': 'offline-p3', 'source_path': str(source), 'paper_latex_root': str(latex),
                   'config': {'model_config_origin': 'v2', 'text_model': 'same-model'}}
            async def subprocess(*argv, **kwargs):
                self.assertIn('--model-config', argv)
                self.assertEqual(argv[argv.index('--model-config') + 1], str(Path(directory, 'model_config.json')))
                self.assertNotIn('offline-text-key', ' '.join(argv))
                latex.mkdir()
                (latex / 'main.tex').touch()
                async def wait():
                    return 0
                return SimpleNamespace(wait=wait)
            with patch.object(web.asyncio, 'create_subprocess_exec', side_effect=subprocess), patch.object(web, '_log_line'), patch.object(web, '_set_phase'):
                self.assertTrue(asyncio.run(web._prepare_latex(job, directory, None, {})))

    def test_legacy_route_keeps_output_override_and_endpoint(self):
        import camel.agents
        import camel.models
        os.environ['PAPER2VIDEO_MODEL_CONFIG_ORIGIN'] = 'legacy'
        os.environ['OPENAI_COMPATIBLE_MODEL_API_BASE'] = 'http://localhost:9010/v1'
        config = wei_utils.get_agent_config('deepseek-flash', 'text', {'model_config_origin': 'legacy'})
        self.assertEqual(config['model_config']['max_tokens'], 65536)
        with patch('camel.models.ModelFactory.create', side_effect=lambda **kwargs: SimpleNamespace(**kwargs)), \
             patch('camel.agents.ChatAgent', side_effect=lambda **kwargs: kwargs['model']):
            client = wei_utils.create_agent(config)
        self.assertEqual(client.api_key, 'forbidden-global-key')
        self.assertEqual(client.url, 'http://localhost:9010/v1')

    def test_course_and_review_generation_share_vision_config_and_cache_identity(self):
        vision = wei_utils.get_agent_config('ignored', 'vision', self.snapshot)
        calls = []
        def generator(*args, **kwargs):
            calls.append(args[2])
            return '讲解 | no', {'total_tokens': 1}
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            slides = Path(directory, 'slides')
            slides.mkdir()
            (slides / '0.png').write_bytes(b'offline-slide')
            prompt = Path(directory, 'prompt.txt')
            prompt.write_text('prompt', encoding='utf-8')
            output = Path(directory, 'output')
            output.mkdir()
            (output / 'subtitle_w_cursor.txt').write_text('原讲稿 | no', encoding='utf-8')
            course.generate_selected(str(slides), str(prompt), vision, str(output), generator, [0], workers=2, generation_id='review')
            self.assertIs(calls[0], vision)
            cached = (output / 'course_script' / '0.json').read_text(encoding='utf-8')
            self.assertNotIn('offline-vision-key', cached)
            course.generate_selected(str(slides), str(prompt), vision, str(output), generator, [0], generation_id='review')
            self.assertEqual(len(calls), 1)
            altered = {**vision, 'scope_id': 'another-account'}
            course.generate_selected(str(slides), str(prompt), altered, str(output), generator, [0], generation_id='review')
            self.assertEqual(len(calls), 2)

    def test_course_plan_uses_same_vision_parameters(self):
        vision = wei_utils.get_agent_config('ignored', 'vision', self.snapshot)
        with tempfile.TemporaryDirectory(dir=ROOT) as directory:
            slides = Path(directory, 'slides')
            slides.mkdir()
            for index in range(4):
                (slides / f'{index}.png').write_bytes(b'offline-slide')
            prompt = Path(directory, 'prompt.txt')
            prompt.write_text('prompt', encoding='utf-8')
            plan, _ = course.build_plan('课程材料，介绍向量、矩阵和线性变换。', {'section_ids': ['all']}, 4, 'zh')
            def generator(*args, **kwargs):
                self.assertIs(args[2], vision)
                self.assertIn('教材原文', kwargs['prompt_text'])
                return '讲解 | no', {}
            content, _ = course.generate_script(plan, str(slides), str(prompt), vision, str(Path(directory, 'output')), generator, workers=2)
            self.assertEqual(len(course.split_pages(content)), 4)

    def test_artifact_fingerprints_include_v2_roles_but_preserve_legacy_keys(self):
        from scripts import artifact_cache
        with patch.object(web, '_figures_signature', return_value='offline-figures'):
            job = {'id': 'offline-cache', 'source_digest': 'source', 'config': {**self.snapshot, 'text_model': 'same-model', 'vision_model': 'same-model'}}
            first = web._artifact_facts(job)
            job['config'] = copy.deepcopy(job['config'])
            job['config']['model_config']['stages']['vision']['parameters']['temperature'] = 0.9
            vision = web._artifact_facts(job)
            self.assertEqual(artifact_cache.struct_key(first), artifact_cache.struct_key(vision))
            self.assertNotEqual(artifact_cache.deck_key(first), artifact_cache.deck_key(vision))
            job['config']['model_config']['stages']['text']['parameters']['max_output_tokens'] = 8192
            text = web._artifact_facts(job)
            self.assertNotEqual(artifact_cache.struct_key(first), artifact_cache.struct_key(text))
            self.assertNotIn('offline-text-key', json.dumps(text))
            self.assertIsNone(text['model_api_base'])
            old = {'source_sha1': 'source', 'llm_model': 'same-model'}
            fields = tuple(name for name in artifact_cache.STRUCT_FIELDS if name != 'text_backend')
            self.assertEqual(artifact_cache.struct_key(old), artifact_cache._key(old, fields))
            fields = tuple(name for name in artifact_cache.DECK_FIELDS if name not in ('text_backend', 'vision_backend'))
            self.assertEqual(artifact_cache.deck_key(old), artifact_cache._key(old, fields))

    def test_frozen_limit_annotations_do_not_mutate_source_task(self):
        store = ConnectionStore()
        connection = store.create(ConnectionCreate(label='文字', provider='openai_compatible', compatible_base_url='http://localhost:9001/v1', api_key='offline-text-key'))
        config = copy.deepcopy(self.config)
        config.stages.text.connection_id = config.stages.vision.connection_id = connection['id']
        snapshots, keys = store.freeze(config, ['text', 'vision'])
        snapshots[connection['id']]['model_limits'] = {'same-model': {'max_output_tokens': 8192}}
        frozen, _ = store.freeze(config, ['text', 'vision'], snapshots, keys)
        frozen[connection['id']]['model_limits']['same-model']['max_output_tokens'] = 16384
        self.assertEqual(snapshots[connection['id']]['model_limits']['same-model']['max_output_tokens'], 8192)

    def test_real_camel_sdk_body_on_mock_transport_only(self):
        import httpx
        from openai import OpenAI, AsyncOpenAI
        from camel.models import ModelFactory
        settings = wei_utils.get_agent_config('ignored', 'vision', self.snapshot)
        received = []
        def respond(request):
            received.append(request)
            return httpx.Response(200, json={'id': 'offline', 'object': 'chat.completion', 'created': 0, 'model': 'same-model',
                'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': 'offline'}, 'finish_reason': 'stop'}],
                'usage': {'prompt_tokens': 1, 'completion_tokens': 1, 'total_tokens': 2}})
        with OpenAI(api_key='offline-vision-key', base_url=settings['url'], http_client=httpx.Client(transport=httpx.MockTransport(respond))) as sdk:
            async_sdk = AsyncOpenAI(api_key='offline-vision-key', base_url=settings['url'], http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond)))
            try:
                model = ModelFactory.create(model_platform=settings['model_platform'], model_type=settings['model_type'],
                    model_config_dict={**settings['model_config'], 'extra_body': {'enable_thinking': False}},
                    api_key='offline-vision-key', url=settings['url'], client=sdk, async_client=async_sdk)
                response = model.run([{'role': 'user', 'content': [{'type': 'text', 'text': 'offline'},
                    {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,b2ZmbGluZQ=='}}]}])
                self.assertEqual(response.choices[0].message.content, 'offline')
            finally:
                asyncio.run(async_sdk.close())
        request = received[0]
        self.assertEqual(str(request.url), 'http://localhost:9002/v1/chat/completions')
        self.assertEqual(request.headers['authorization'], 'Bearer offline-vision-key')
        body = json.loads(request.content)
        self.assertEqual((body['model'], body['max_tokens'], body['temperature'], body['enable_thinking']), ('same-model', 4096, 0.8, False))
        self.assertEqual(body['messages'][0]['content'][1]['type'], 'image_url')


if __name__ == '__main__':
    unittest.main()
