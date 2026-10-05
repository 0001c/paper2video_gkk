"""P5/P6与计费离线回归；只使用模拟HTTP、生成的PCM和ffmpeg色块视频。"""

import copy
import json
import os
import subprocess
import sys
import tempfile
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'Paper2Video-main' / 'src')]

import httpx
from PIL import Image
import avatar_segments as segments
import bailian_grounding as grounding
import bailian_speech as speech
import cursor_gen
from web import model_costs
from web.model_config import ConfigurationError, ensure_implemented, legacy_config


def snapshot():
    config = legacy_config({'generate_talking': False}, 'test')
    config.stages.grounding.mode = 'api'
    config.stages.grounding.provider = 'bailian'
    config.stages.grounding.model = 'qwen3-vl-plus'
    config.stages.grounding.connection_id = 'ground'
    return {'model_config_origin': 'v2', 'model_config': config.public_dict(), 'model_connections': {
        'ground': {'provider': 'bailian', 'scope_id': 'fake-space', 'compatible_base_url': 'https://test.cn-beijing.maas.aliyuncs.com/compatible-mode/v1'}}}


def wav(filename, samples, rate=1000):
    with wave.open(str(filename), 'wb') as handle:
        handle.setparams((1, 2, rate, samples, 'NONE', 'not compressed'))
        handle.writeframes(bytes(samples * 2))


class Checks(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.environment = patch.dict(os.environ, {'PAPER2VIDEO_ROLE_GROUNDING_API_KEY': 'fake-ground-key',
            'PAPER2VIDEO_USAGE_DIR': str(self.root / 'costs')})
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def test_point_and_bbox_transform(self):
        for field, value in [('point', [500, 250]), ('bbox', [400, 200, 600, 300])]:
            payload = {'found': True, 'coordinate_scale': 1000, 'point': None, 'bbox': None, field: value}
            self.assertEqual(grounding.parse_location(json.dumps(payload), 1001, 501), [500, 125])
        self.assertIsNone(grounding.parse_location('{"found":false,"coordinate_scale":1000,"point":null,"bbox":null}', 100, 100))

    def test_parser_rejects_commands_and_invalid_coordinates(self):
        for content in ['__import__("os").system("echo unsafe")', 'click(10,20)', '{"found":true,"coordinate_scale":1000,"point":[1001,0],"bbox":null}',
                        '{"found":true,"coordinate_scale":1000,"point":[NaN,0],"bbox":null}', '{"found":true,"coordinate_scale":1000,"point":[true,0],"bbox":null}',
                        '{"found":true,"coordinate_scale":1000,"point":[0,0],"bbox":null,"command":"click"}',
                        '{"found":false,"found":true,"coordinate_scale":1000,"point":null,"bbox":null}']:
            with self.subTest(content=content), self.assertRaises((ValueError, TypeError)):
                grounding.parse_location(content, 100, 100)

    def test_request_independent_key_and_usage(self):
        image = self.root / 'slide.png'
        Image.new('RGB', (1600, 800)).save(image)
        def handler(request):
            self.assertEqual(request.headers['Authorization'], 'Bearer fake-ground-key')
            data = json.loads(request.content)
            self.assertFalse(data['enable_thinking'])
            self.assertEqual(data['max_tokens'], 512)
            return httpx.Response(200, json={'choices': [{'finish_reason': 'stop', 'message': {'content': '{"found":true,"coordinate_scale":1000,"point":[500,500],"bbox":null}'}}], 'usage': {'prompt_tokens': 100, 'completion_tokens': 10}})
        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            point, content = grounding.infer('red box', image, snapshot(), client)
        self.assertEqual(point, [800, 400])
        report = model_costs.summary(self.root / 'costs')
        self.assertEqual(report['known_estimated_cny'], 0.0002)
        self.assertNotIn('fake-ground-key', json.dumps(report))

    def test_missing_key_and_rejected_response(self):
        image = self.root / 'slide.png'
        Image.new('RGB', (100, 100)).save(image)
        with patch.dict(os.environ, {'PAPER2VIDEO_ROLE_GROUNDING_API_KEY': ''}), self.assertRaises(ConfigurationError):
            grounding.infer('x', image, snapshot())
        with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(403))) as client, self.assertRaises(ValueError):
            grounding.infer('x', image, snapshot(), client)
        self.assertEqual(model_costs.summary(self.root / 'costs')['unknown_requests'], 1)

    def test_cursor_api_cache_scope_and_no_gpu(self):
        image_dir, audio_dir = self.root / 'images', self.root / 'audio'
        image_dir.mkdir(); audio_dir.mkdir()
        Image.new('RGB', (200, 100)).save(image_dir / '1.png')
        script = self.root / 'script.txt'
        script.write_text('hello|red box', encoding='utf-8')
        speech.write_pair(audio_dir / '0.wav', bytes(24000 * 2), {'segments': [{'words': [{'word': 'hello', 'start': 0.1, 'end': 0.8}]}]}, 'hello')
        output = str(self.root / 'cursor.json')
        config = snapshot()
        with patch.object(grounding, 'infer', return_value=([50, 25], 'json')) as infer, patch.object(cursor_gen, 'get_gui_model', side_effect=AssertionError('GPU')), patch('speech_gen.align_with_qwen3', side_effect=AssertionError('align')):
            for attempt in range(2):
                cursor_gen.cursor_gen_per_sentence(str(script), str(image_dir), str(audio_dir), output, resume_cache=True, timing_source='tts_timestamp', model_snapshot=config)
            self.assertEqual(infer.call_count, 1)
            config['model_connections']['ground']['scope_id'] = 'other-space'
            cursor_gen.cursor_gen_per_sentence(str(script), str(image_dir), str(audio_dir), output, resume_cache=True, timing_source='tts_timestamp', model_snapshot=config)
            self.assertEqual(infer.call_count, 2)
        self.assertEqual(json.loads(Path(output).read_text())[0]['cursor'], [50, 25])

    def test_timestamp_words_not_uniform_sentence_interpolation(self):
        timing = {'segments': [{'words': [{'word': 'helloworld', 'start': 0.2, 'end': 2.0}]}]}
        result = grounding.timestamp_sentences(['hello', 'world'], timing, 3.0)
        self.assertEqual(result[0]['end'], 2.0)
        self.assertEqual(result[1]['start'], 2.0)
        with self.assertRaises(ValueError):
            grounding.timestamp_sentences(['mismatch'], timing, 3.0)

    def test_disabled_does_not_read_audio(self):
        self.assertIsNone(segments.prepare('missing.wav', self.root, enabled=False))

    def test_exact_sample_boundaries_and_coverage(self):
        for count, expected in [(19999, 1), (20000, 2), (20001, 2), (45000, 3), (36000, 2)]:
            audio = self.root / ('{}.wav'.format(count))
            wav(audio, count)
            manifest = segments.prepare(audio, self.root / 'parts')
            self.assertEqual(len(manifest['segments']), expected)
            self.assertEqual(sum(item['sample_count'] for item in manifest['segments']), count)
            self.assertEqual(manifest['segments'][0]['start_sample'], 0)
            self.assertEqual(manifest['segments'][-1]['end_sample'], count)
            for item in manifest['segments']:
                self.assertLess(segments.verify_segment(item['file']), 20)
            rebuilt = b''.join(segments.audio_data(item['file'])[4] for item in manifest['segments'])
            self.assertEqual(rebuilt, segments.audio_data(audio)[4])

    def test_segment_cache_repair_and_natural_word_boundary(self):
        audio = self.root / 'source.wav'
        wav(audio, 21000)
        timing = {'text': '你好', 'normalization_map': [{'original_index': 0, 'normalized': '你'}, {'original_index': 1, 'normalized': '好'}],
                  'segments': [{'words': [{'word': '你', 'start': 0, 'end': 17.8}, {'word': '好', 'start': 17.8, 'end': 21}]}]}
        manifest = segments.prepare(audio, self.root / 'parts', timing=timing)
        self.assertEqual(manifest['segments'][0]['end_sample'], 17800)
        self.assertEqual(manifest['segments'][0]['text_range'], [0, 1])
        filename = Path(manifest['segments'][0]['file'])
        before = filename.stat().st_mtime_ns
        segments.prepare(audio, self.root / 'parts', timing=timing)
        self.assertEqual(filename.stat().st_mtime_ns, before)
        filename.write_bytes(b'corrupt')
        segments.prepare(audio, self.root / 'parts', timing=timing)
        self.assertEqual(segments.verify_segment(filename), 17.8)

    def test_strict_upload_guard(self):
        audio = self.root / 'long.wav'
        wav(audio, 20000)
        with self.assertRaises(ValueError):
            segments.verify_segment(audio)

    def test_ffmpeg_stitch_master_audio_single_track(self):
        audio = self.root / 'source.wav'
        wav(audio, 21000)
        manifest = segments.prepare(audio, self.root / 'parts')
        videos = []
        for item in manifest['segments']:
            video = self.root / ('{}.mp4'.format(item['index']))
            subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y', '-f', 'lavfi', '-i', 'color=c=blue:s=64x64:r=25', '-t', str(item['sample_count'] / 1000), '-c:v', 'libx264', '-pix_fmt', 'yuv420p', str(video)], check=True, capture_output=True)
            videos.append(video)
        output = segments.stitch(videos, manifest, audio, self.root / 'page.mp4')
        self.assertTrue(Path(output).exists())
        with self.assertRaises(ValueError):
            segments.stitch([], manifest, audio, self.root / 'bad.mp4')

    def test_price_tiers_unknown_and_local_no_bill(self):
        for prompt, expected in [(32000, 0.033), (32001, 0.0495015), (128000, 0.1935), (128001, 0.387003), (256000, 0.771)]:
            ticket = model_costs.begin('vision', 'qwen3-vl-plus', 'https://dashscope.aliyuncs.com/v1')
            model_costs.finish(ticket, {'prompt_tokens': prompt, 'completion_tokens': 100})
            self.assertAlmostEqual(ticket[1]['estimated_cny'], expected)
        ticket = model_costs.begin('vision', 'qwen3-vl-plus', 'https://dashscope.aliyuncs.com/v1')
        model_costs.finish(ticket, {'prompt_tokens': 256001, 'completion_tokens': 100})
        self.assertIsNone(ticket[1]['estimated_cny'])
        for model in ('qwen3.8-flash', 'qwen3.8-max'):
            ticket = model_costs.begin('text', model, 'https://dashscope.aliyuncs.com/v1')
            model_costs.finish(ticket, {'prompt_tokens': 1000000, 'completion_tokens': 100})
            self.assertIsNotNone(ticket[1]['estimated_cny'])
            ticket = model_costs.begin('text', model, 'https://dashscope.aliyuncs.com/v1')
            model_costs.finish(ticket, {'prompt_tokens': 1000001, 'completion_tokens': 100})
            self.assertIsNone(ticket[1]['estimated_cny'])
        ticket = model_costs.begin('text', 'unknown-model', 'https://dashscope.aliyuncs.com/v1')
        model_costs.finish(ticket, {'prompt_tokens': 10, 'completion_tokens': 10})
        self.assertIsNone(ticket[1]['estimated_cny'])
        ticket = model_costs.begin('tts', 'cosyvoice-v3-flash', 'https://dashscope.aliyuncs.com/api/v1')
        model_costs.finish(ticket, {'characters': 100})
        self.assertEqual(ticket[1]['estimated_cny'], 0.01)

    def test_usage_wrap_all_attempts_including_invalid_content(self):
        import wei_utils
        import camel.agents
        import camel.models
        config = {'model_type': 'qwen3.8-flash', 'model_platform': 'OPENAI_COMPATIBLE_MODEL', 'provider': 'bailian', 'role': 'text',
                  'api_key_env': 'PAPER2VIDEO_ROLE_GROUNDING_API_KEY', 'url': 'https://dashscope.aliyuncs.com/v1'}
        agent = SimpleNamespace(step=lambda *args: SimpleNamespace(info={'usage': {'prompt_tokens': 100, 'completion_tokens': 20}}))
        with patch.object(camel.agents, 'ChatAgent', return_value=agent), patch.object(camel.models.ModelFactory, 'create') as factory:
            measured = wei_utils.create_agent(config)
            measured.step('test'); measured.step('retry')
            self.assertEqual(factory.call_args.kwargs['max_retries'], 0)
        group = next(item for item in model_costs.summary(self.root / 'costs')['stages'] if item['role'] == 'text')
        self.assertEqual(group['requests'], 2)
        self.assertEqual(group['usage']['prompt_tokens'], 200)

    def test_unsupported_wan_remains_blocked(self):
        config = legacy_config({'generate_talking': True}, 'test')
        ensure_implemented(config, ['avatar'])
        config.avatar.mode = 'api'; config.avatar.provider = 'bailian'; config.avatar.model = 'wan2.2-s2v'; config.avatar.connection_id = 'test'
        with self.assertRaises(ConfigurationError):
            ensure_implemented(config, ['avatar'])

    def test_usage_unknown_attempt_and_unknown_region_not_zero(self):
        ticket = model_costs.begin('tts', 'cosyvoice-v3-flash', 'https://dashscope.aliyuncs.com/api/v1')
        report = model_costs.summary(self.root / 'costs')
        self.assertEqual(report['unknown_requests'], 1)
        self.assertIsNone(ticket[1]['estimated_cny'])
        ticket = model_costs.begin('tts', 'cosyvoice-v3-flash', 'https://test.dashscope.aliyuncs.com/api/v1')
        model_costs.finish(ticket, {'characters': 100})
        self.assertIsNone(ticket[1]['estimated_cny'])

    def test_cost_endpoints_are_read_only(self):
        from fastapi.testclient import TestClient
        from web import app
        client = TestClient(app.app, client=('127.0.0.1', 9000))
        with patch.object(app, '_load_job', return_value={'id': 'fake', 'config': {'result_dir': str(self.root)}}):
            result = client.get('/api/jobs/fake/model-costs')
            self.assertEqual(result.status_code, 200)
            self.assertEqual(result.json()['unknown_requests'], 0)
            self.assertFalse((self.root / 'model_usage').exists())
            self.assertEqual(client.get('/api/model-costs/voices').status_code, 200)

    def test_local_avatar_dispatch_only_short_inputs(self):
        import talking_gen
        audio, reference = self.root / 'long.wav', self.root / 'ref.png'
        wav(audio, 45000)
        Image.new('RGB', (64, 64)).save(reference)
        seen = []
        def generate(task):
            seen.append(segments.verify_segment(task[2]))
            return task[0], True, 'mock'
        with patch.object(talking_gen, 'resolve_profile', return_value={'backend': 'musetalk', 'batch': False}), patch.object(talking_gen, 'describe_profile', return_value='mock'), patch.object(talking_gen, 'run_avatar_inference', side_effect=generate), patch.object(segments, 'stitch') as stitch:
            results = talking_gen.talking_gen_per_slide('musetalk', [(str(reference), str(audio))], str(self.root / 'talking'), [], 'unused')
            self.assertEqual(seen, [18, 18, 9])
            self.assertTrue(results[0][1])
            self.assertEqual(stitch.call_args.args[2], str(audio))

    def test_short_video_is_not_padded_to_hide_failure(self):
        audio = self.root / 'source.wav'
        wav(audio, 1000)
        manifest = segments.prepare(audio, self.root / 'parts')
        probe = SimpleNamespace(stdout=b'{"streams":[{"duration":"0.1"}]}')
        with patch.object(subprocess, 'run', return_value=probe), self.assertRaises(ValueError):
            segments.stitch(['too-short.mp4'], manifest, audio, self.root / 'out.mp4')

    def test_live_sse_punctuation_updates_preserve_spoken_words_and_audio(self):
        import base64
        events = []
        pcm = bytes(24000 * 2)
        events.append({'output': {'audio': {'data': base64.b64encode(pcm).decode()}, 'sentence': {'index': 0, 'words': [
            {'text': '你', 'begin_time': 0, 'end_time': 200}, {'text': '，', 'begin_time': 200, 'end_time': 300}]}}})
        events.append({'output': {'sentence': {'index': 0, 'words': [{'text': '，', 'begin_time': 200, 'end_time': 400},
            {'text': '好', 'begin_time': 400, 'end_time': 600}, {'text': '。', 'begin_time': 600, 'end_time': 700}]}}})
        events.append({'output': {'finish_reason': 'stop', 'sentence': {'index': 0, 'words': [{'text': '。', 'begin_time': 600, 'end_time': 1000}]}}, 'usage': {'characters': 4}})
        body = ''.join('data: ' + json.dumps(event) + '\n\n' for event in events)
        connection = {'native_http_base_url': 'https://dashscope.aliyuncs.com/api/v1'}
        with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, text=body))) as client:
            result, timing = speech.SpeechClient(connection, 'fake-key', client).synthesize('cosyvoice-v3-flash', 'longanyang', '你，好。')
        words = timing['segments'][0]['words']
        self.assertEqual(words, [{'word': '你', 'start': 0, 'end': 0.2},
                                 {'word': '好', 'start': 0.4, 'end': 0.6}])
        self.assertEqual(result, pcm)

    def test_structured_input_model_usage_is_counted(self):
        sys.path.insert(0, str(ROOT / 'scripts'))
        import paper_struct
        config = snapshot()
        config['model_config']['stages']['text'].update(mode='api', provider='bailian', model='qwen3.8-flash', connection_id='ground')
        response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='test', reasoning_content=None), finish_reason='stop')],
                                   usage=SimpleNamespace(prompt_tokens=100, completion_tokens=10, total_tokens=110))
        with patch.dict(os.environ, {'PAPER2VIDEO_ROLE_TEXT_API_KEY': 'fake-text-key'}), patch('openai.OpenAI') as client:
            client.return_value.__enter__.return_value.chat.completions.create.return_value = response
            paper_struct.call_llm('system', 'user', model_snapshot=config)
            self.assertEqual(client.call_args.kwargs['max_retries'], 0)
        group = next(item for item in model_costs.summary(self.root / 'costs')['stages'] if item['role'] == 'text')
        self.assertEqual(group['requests'], 1)
        self.assertEqual(group['usage']['total_tokens'], 110)


if __name__ == '__main__':
    unittest.main(verbosity=2)
