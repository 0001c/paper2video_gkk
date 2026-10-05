"""P4离线协议、配对缓存、音色范围与防重回归。"""

import base64
import asyncio
import json
import os
import sys
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch, AsyncMock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'Paper2Video-main' / 'src'))

import httpx
import bailian_speech as speech
from web.cloud_voices import VoiceStore, VoiceRegister, VoicePreview, VoiceGenerate
from web.model_config import ConfigurationError
from web.model_config import legacy_config
from web.model_connections import ConnectionStore, ConnectionCreate


CONNECTION = {'scope_id': 'test-space', 'provider': 'bailian', 'api_key': 'fake-test-key',
              'native_http_base_url': 'https://test.cn-beijing.maas.aliyuncs.com/api/v1'}
PCM = bytes(24000 * 2)


def stream_event(text='hello', finished=True, words=True):
    output = {'audio': {'data': base64.b64encode(PCM).decode()},
              'sentence': {'index': 0, 'words': [{'text': text, 'begin_time': 0, 'end_time': 1000}] if words else []},
              'finish_reason': 'stop' if finished else None}
    return 'data: ' + json.dumps({'output': output}) + '\n\n'


def mock_client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


class SpeechTests(unittest.TestCase):
    def test_sse_protocol_and_no_global_key(self):
        requests = []
        def handler(request):
            requests.append(request)
            return httpx.Response(200, text=stream_event())
        with mock_client(handler) as client:
            pcm, timing = speech.SpeechClient(CONNECTION, 'fake-role-key', client).synthesize('cosyvoice-v3-flash', 'longanyang', 'hello', 0.8, 'en')
        self.assertEqual(pcm, PCM)
        self.assertEqual(timing['segments'][0]['words'][0]['end'], 1)
        self.assertEqual(str(requests[0].url), CONNECTION['native_http_base_url'] + '/services/audio/tts/SpeechSynthesizer')
        self.assertEqual(requests[0].headers['Authorization'], 'Bearer fake-role-key')
        body = json.loads(requests[0].content)
        self.assertEqual(body['input']['rate'], 0.8)
        self.assertTrue(body['input']['word_timestamp_enabled'])
        self.assertEqual(body['input']['format'], 'pcm')

    def test_missing_key_endpoint_and_wrong_region(self):
        for connection, key in ((CONNECTION, ''), ({}, 'fake'),
                                ({**CONNECTION, 'native_http_base_url': 'https://test.ap-southeast-1.maas.aliyuncs.com/api/v1'}, 'fake')):
            with self.assertRaises((ValueError, ConfigurationError)):
                speech.SpeechClient(connection, key)

    def test_incomplete_or_missing_timestamps(self):
        for finished, words in ((False, True), (True, False)):
            with mock_client(lambda request: httpx.Response(200, text=stream_event(finished=finished, words=words))) as client:
                with self.assertRaises(ValueError):
                    speech.SpeechClient(CONNECTION, 'fake', client).synthesize('cosyvoice-v3-flash', 'longanyang', 'hello')

    def test_incremental_word_events_merge_without_duplicate(self):
        events = []
        for index, word in enumerate(('hello', 'world')):
            output = {'sentence': {'index': 0, 'words': [{'text': word, 'begin_time': index * 500, 'end_time': (index + 1) * 500}]},
                      'audio': {'data': base64.b64encode(PCM).decode() if index == 0 else ''}, 'finish_reason': 'stop' if index == 1 else None}
            events.append('data: ' + json.dumps({'output': output}) + '\n\n')
        with mock_client(lambda request: httpx.Response(200, text=''.join(events) + 'data: [DONE]\n\n')) as client:
            pcm, timing = speech.SpeechClient(CONNECTION, 'fake', client).synthesize('cosyvoice-v3-flash', 'longanyang', 'hello world')
        self.assertEqual([word['word'] for word in timing['segments'][0]['words']], ['hello', 'world'])

    def test_cursor_time_axis_can_consume_cloud_pair(self):
        from cursor_gen import timesteps
        with tempfile.TemporaryDirectory() as directory:
            filename = Path(directory, '0.wav')
            timing = {'segments': [{'words': [{'word': 'hello', 'start': 0, 'end': 0.5}, {'word': 'world', 'start': 0.5, 'end': 1}]}]}
            speech.write_pair(filename, PCM, timing, 'hello world')
            pcm, sidecar = speech.read_pair(filename)
            result = timesteps(['hello', 'world'], sidecar, str(filename))
            self.assertEqual(len(result), 2)
            self.assertEqual(result[-1]['end'], 1)

    def test_overlapping_punctuation_does_not_corrupt_word_timing(self):
        words = [{'text': 'hello', 'begin_time': 0, 'end_time': 400},
                 {'text': '。', 'begin_time': 400, 'end_time': 510},
                 {'text': '。。', 'begin_time': 400, 'end_time': 680},
                 {'text': '。', 'begin_time': 510, 'end_time': 620},
                 {'text': 'world', 'begin_time': 700, 'end_time': 1000}]
        output = {'audio': {'data': base64.b64encode(PCM).decode()},
                  'sentence': {'index': 0, 'words': words}, 'finish_reason': 'stop'}
        event = 'data: ' + json.dumps({'output': output}) + '\n\n'
        with mock_client(lambda request: httpx.Response(200, text=event)) as client:
            pcm, timing = speech.SpeechClient(CONNECTION, 'fake', client).synthesize('cosyvoice-v3-flash', 'longanyang', 'hello。\n\n\nworld')
        self.assertEqual(timing['segments'][0]['words'], [
            {'word': 'hello', 'start': 0, 'end': 0.4},
            {'word': 'world', 'start': 0.7, 'end': 1.0}])
        self.assertEqual(pcm, PCM)

    def test_overlapping_spoken_words_still_fail(self):
        output = {'audio': {'data': base64.b64encode(PCM).decode()},
                  'sentence': {'index': 0, 'words': [
                      {'text': 'hello', 'begin_time': 0, 'end_time': 600},
                      {'text': 'world', 'begin_time': 500, 'end_time': 1000}]},
                  'finish_reason': 'stop'}
        event = 'data: ' + json.dumps({'output': output}) + '\n\n'
        with mock_client(lambda request: httpx.Response(200, text=event)) as client:
            with self.assertRaisesRegex(ValueError, '时间戳越界或不单调'):
                speech.SpeechClient(CONNECTION, 'fake', client).synthesize('cosyvoice-v3-flash', 'longanyang', 'hello world')

    def test_timing_bounds_and_mapping(self):
        for start, end, text in ((-1, 1, 'hello'), (0, 2, 'hello'), (0, float('nan'), 'hello'), (0, 1, 'different')):
            with self.assertRaises(ValueError):
                speech.validate_timing({'segments': [{'words': [{'word': text, 'start': start, 'end': end}]}]}, PCM, 'hello')

    def test_audio_timing_pair_hashes(self):
        with tempfile.TemporaryDirectory() as directory:
            filename = Path(directory, '0.wav')
            speech.write_pair(filename, PCM, {'segments': [{'words': [{'word': 'hello', 'start': 0, 'end': 1}]}]}, 'hello')
            pcm, timing = speech.read_pair(filename, 'hello')
            self.assertEqual(pcm, PCM)
            self.assertEqual(timing['sample_count'], 24000)
            filename.write_bytes(filename.read_bytes()[:-2] + b'xx')
            with self.assertRaises(ValueError):
                speech.read_pair(filename, 'hello')

    def snapshot(self):
        return {'model_config_origin': 'v2', 'model_config': {'stages': {'tts': {'mode': 'api', 'provider': 'bailian',
                'model': 'cosyvoice-v3-flash', 'connection_id': 'fake', 'parameters': {'speech_rate': 1.0}}}},
                'model_connections': {'fake': CONNECTION}, 'voice_resource': {'target_model': 'cosyvoice-v3-flash',
                'scope_id': CONNECTION['scope_id'], 'native_http_base_url': CONNECTION['native_http_base_url'],
                'voice_id': 'longanyang', 'timestamp_status': 'supported', 'status': 'OK'}}

    def test_page_chunks_offsets_and_resume(self):
        calls = []
        class FakeSpeech:
            def synthesize(self, model, voice, text, rate, language):
                calls.append(text)
                return PCM, {'segments': [{'words': [{'word': text, 'start': 0, 'end': 1}]}]}
        with tempfile.TemporaryDirectory() as directory:
            text = '甲' * 400 + '乙' * 20
            snapshot = self.snapshot()
            speech.tts_pages([[(text, 'cursor')]], directory, snapshot, client=FakeSpeech())
            pcm, timing = speech.read_pair(Path(directory, '0.wav'), text)
            self.assertEqual(len(pcm) // 2, int(2.35 * 24000))
            self.assertAlmostEqual(timing['segments'][1]['words'][0]['start'], 1.35)
            speech.tts_pages([[(text, 'cursor')]], directory, snapshot, client=FakeSpeech())
            self.assertEqual(len(calls), 2)
            Path(directory, '0.timing.json').unlink()
            speech.tts_pages([[(text, 'cursor')]], directory, snapshot, client=FakeSpeech())
            self.assertEqual(len(calls), 2)
            self.assertTrue(Path(directory, '0.timing.json').exists())

    def test_unknown_submission_not_resubmitted(self):
        calls = []
        class BrokenSpeech:
            def synthesize(self, *args):
                calls.append(1)
                raise ValueError('interrupted')
        with tempfile.TemporaryDirectory() as directory:
            for attempt in range(2):
                with self.assertRaises(ValueError):
                    speech.tts_pages([[('hello', 'cursor')]], directory, self.snapshot(), client=BrokenSpeech())
            self.assertEqual(len(calls), 1)

    def test_voice_scope_and_design_gate(self):
        for field, value in (('scope_id', 'other'), ('target_model', 'cosyvoice-v2'), ('timestamp_status', 'unknown')):
            snapshot = self.snapshot()
            snapshot['voice_resource'][field] = value
            with self.assertRaises(ValueError):
                speech.tts_pages([], '.', snapshot)

    def test_cloud_dispatch_does_not_load_local_models(self):
        import speech_gen
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory, 'script.txt')
            script.write_text('hello|cursor', encoding='utf-8')
            with patch('bailian_speech.tts_pages') as pages, patch.object(speech_gen, 'get_tts_model', side_effect=AssertionError('local load')), patch.object(speech_gen, 'transcribe_with_qwen3asr', side_effect=AssertionError('local ASR')):
                speech_gen.tts_per_slide('cosyvoice-v3-flash', str(script), directory, None, model_snapshot=self.snapshot())
                pages.assert_called_once()


class VoiceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.uploads = self.root / 'uploads'
        self.uploads.mkdir()
        self.store = VoiceStore(self.root / 'voices', self.uploads)

    def tearDown(self):
        self.temporary.cleanup()

    def test_register_scope_and_unverified(self):
        payload = VoiceRegister(connection_id='fake', target_model='cosyvoice-v3-flash', voice_id='cosyvoice-v3-flash-p2v-example', label='test')
        result = self.store.register(payload, CONNECTION)
        self.assertEqual(result['status'], 'unverified')
        self.assertEqual(result['timestamp_status'], 'unknown')
        with self.assertRaises(ConfigurationError):
            self.store.get(result['resource_id'], {**CONNECTION, 'scope_id': 'other'}, payload.target_model)
        self.assertEqual(len(self.store.listing(CONNECTION, 'cosyvoice-v3-flash')), 2)

    def test_durable_idempotency_conflict_and_unknown(self):
        calls = []
        operation = lambda: calls.append(1) or {'ok': True}
        for attempt in range(2):
            self.store.action(CONNECTION, 'test-key-123', {'input': 1}, operation)
            self.store = VoiceStore(self.root / 'voices', self.uploads)
        self.assertEqual(len(calls), 1)
        with self.assertRaises(ConfigurationError):
            self.store.action(CONNECTION, 'test-key-123', {'input': 2}, operation)
        with self.assertRaises(ValueError):
            self.store.action(CONNECTION, 'unknown-key-123', {}, lambda: (_ for _ in ()).throw(ValueError('interrupted')))
        with self.assertRaises(ConfigurationError):
            self.store.action(CONNECTION, 'unknown-key-123', {}, operation)

    def test_refresh_whitelist_and_preview_probe(self):
        resource = self.store.register(VoiceRegister(connection_id='fake', target_model='cosyvoice-v3-flash', voice_id='cosyvoice-v3-flash-p2v-example', label='test'), CONNECTION)
        with mock_client(lambda request: httpx.Response(200, json={'output': {'status': 'OK', 'target_model': resource['target_model'], 'resource_link': 'https://signed.example?secret=bad'}})) as client:
            result = self.store.refresh(resource['resource_id'], CONNECTION, client)
        self.assertEqual(result['status'], 'OK')
        self.assertEqual(result['timestamp_status'], 'unknown')
        with mock_client(lambda request: httpx.Response(200, text=stream_event())) as client:
            result = self.store.preview(resource['resource_id'], VoicePreview(connection_id='fake', target_model=resource['target_model'], text='hello'), CONNECTION, 'preview-key-123', client)
        self.assertEqual(self.store.get(resource['resource_id'], CONNECTION)['timestamp_status'], 'supported')
        self.assertTrue((self.store.directory / (resource['resource_id'] + '.wav')).exists())
        system = next(voice for voice in self.store.listing(CONNECTION, 'cosyvoice-v3-flash') if voice['kind'] == 'system')
        with mock_client(lambda request: httpx.Response(200, text=stream_event())) as client:
            self.store.preview(system['resource_id'], VoicePreview(connection_id='fake', target_model='cosyvoice-v3-flash', text='hello'), CONNECTION, 'system-preview-key-123', client)
        listed = self.store.listing(CONNECTION, 'cosyvoice-v3-flash')
        self.assertEqual(len(listed), 2)
        self.assertTrue(all(voice.get('sample_url') for voice in listed))
        self.assertNotIn('secret=bad', json.dumps(self.store.read()))
        self.assertNotIn(CONNECTION['api_key'], json.dumps(self.store.read()))

    def test_clone_explicit_upload_and_no_signed_url_persistence(self):
        filename = self.uploads / 'reference.wav'
        with wave.open(str(filename), 'wb') as handle:
            handle.setnchannels(1); handle.setsampwidth(2); handle.setframerate(24000); handle.writeframes(bytes(5 * 24000 * 2))
        payload = VoiceGenerate(connection_id='fake', target_model='cosyvoice-v3-flash', kind='clone', label='test', path=str(filename), consent=True)
        uploads = []
        with mock_client(lambda request: httpx.Response(200, json={'output': {'voice_id': 'cosyvoice-v3-flash-p2v-created'}})) as client:
            resource = self.store.generate(payload, CONNECTION, 'clone-key-123', client, lambda path: uploads.append(path) or 'https://private.oss-cn-beijing.aliyuncs.com/ref?Signature=secret')
            self.store.generate(payload, CONNECTION, 'clone-key-123', client, lambda path: uploads.append(path) or 'bad')
        self.assertEqual(len(uploads), 1)
        self.assertEqual(resource['kind'], 'clone')
        self.assertNotIn('Signature=', json.dumps(self.store.read()))
        with self.assertRaises(ConfigurationError):
            self.store.generate(payload.model_copy(update={'path': str(self.root / 'not-owned.wav')}), CONNECTION, 'bad-clone-key', publisher=lambda path: 'bad')

    def test_clone_missing_publisher_does_not_record_submission(self):
        filename = self.uploads / 'reference.wav'
        filename.write_bytes(speech.wav_bytes(bytes(5 * 24000 * 2)))
        payload = VoiceGenerate(connection_id='fake', target_model='cosyvoice-v3-flash', kind='clone', label='test', path=str(filename), consent=True)
        with patch.dict(os.environ, {name: '' for name in ('PAPER2VIDEO_VOICE_OSS_ACCESS_KEY_ID', 'PAPER2VIDEO_VOICE_OSS_ACCESS_KEY_SECRET')}):
            with self.assertRaises(ConfigurationError):
                self.store.generate(payload, CONNECTION, 'missing-publisher-123')
        self.assertFalse(self.store.read()['actions'])


class BackendTests(unittest.TestCase):
    def test_local_cancellation_is_not_claimed_remote_cancel(self):
        from web import app as web
        job = {'status': 'running', 'config': {'model_config_origin': 'v2', 'model_config': {'stages': {'tts': {'mode': 'api'}}}}}
        with patch.object(web, '_load_job', return_value=job), patch.object(web, '_CURRENT', {'proc': None}), patch.object(web, '_update_job', new_callable=AsyncMock) as update:
            response = asyncio.run(web.api_cancel_job('offline-cancel'))
            self.assertEqual(response['remote_cancel_state'], 'unconfirmed')
            self.assertIn('计费', response['warning'])
            self.assertEqual(update.await_args.kwargs['status'], 'canceled')

    def test_cloud_job_snapshot_environment_and_pure_validation(self):
        from web import app as web
        from web.model_catalog import CatalogStore
        store = ConnectionStore()
        entry = store.create(ConnectionCreate(label='测试配音', provider='bailian',
            compatible_base_url='https://test.cn-beijing.maas.aliyuncs.com/compatible-mode/v1',
            native_http_base_url=CONNECTION['native_http_base_url'], api_key='fake-private-key', scope_id='test-space'))
        config = legacy_config({})
        config.stages.tts.mode = 'api'; config.stages.tts.provider = 'bailian'
        config.stages.tts.connection_id = entry['id']; config.stages.tts.model = 'cosyvoice-v3-flash'
        config.timing.source = 'tts_timestamp'
        with tempfile.TemporaryDirectory() as directory:
            voices = VoiceStore(Path(directory, 'voices'), Path(directory, 'uploads'))
            resource = voices.listing(entry, 'cosyvoice-v3-flash')[0]
            job_config = {'stages': ['2'], 'reuse_script': True, 'require_reuse_script': True, 'script_lang': 'en'}
            payload = {'model_config': config.public_dict(), 'voice': {'kind': 'cloud', 'resource_id': resource['resource_id']}}
            with patch.object(web, '_MODEL_CONNECTIONS', store), patch.object(web, '_MODEL_CATALOG', CatalogStore()), patch.object(web, '_CLOUD_VOICES', voices), patch('httpx.Client', side_effect=AssertionError('configuration must not call HTTP')):
                keys = web._configure_job_models(job_config, payload)
                self.assertEqual(job_config['required_model_roles'], ['grounding', 'tts'])
                self.assertEqual(job_config['effective_model_mode'], 'mixed')
                self.assertNotIn('ref_audio', job_config)
                with patch.dict(os.environ, {'PAPER2VIDEO_VOICE_OSS_ACCESS_KEY_SECRET': 'forbidden-oss', 'BAILIAN_API_KEY': 'forbidden-bailian-global'}):
                    environment = web._job_model_environment(job_config, {'connections': keys})
                self.assertEqual(environment['PAPER2VIDEO_ROLE_TTS_API_KEY'], 'fake-private-key')
                self.assertNotIn('PAPER2VIDEO_ROLE_ASR_API_KEY', environment)
                self.assertNotIn('PAPER2VIDEO_VOICE_OSS_ACCESS_KEY_SECRET', environment)
                self.assertNotIn('BAILIAN_API_KEY', environment)
                target = Path(directory, 'test-job'); target.mkdir()
                with patch.object(web, 'JOBS_ROOT', directory):
                    web._save_job({'id': 'test-job', 'config': job_config})
                snapshot = json.loads((target / 'model_config.json').read_text(encoding='utf-8'))
                self.assertEqual(snapshot['voice_resource']['voice_id'], 'longanyang')
                self.assertNotIn('fake-private-key', json.dumps(snapshot))

    def test_api_voice_endpoints_auth_schema_and_no_get_synthesis(self):
        from web import app as web
        from fastapi.testclient import TestClient
        client = TestClient(web.app, client=('127.0.0.1', 52000))
        self.addCleanup(client.close)
        with patch.object(web._CLOUD_VOICES, 'connection', return_value=CONNECTION), patch.object(web._CLOUD_VOICES, 'listing', return_value=[]), patch.object(speech.SpeechClient, 'synthesize', side_effect=AssertionError('GET cannot synthesize')):
            self.assertEqual(client.get('/api/cloud-voices?connection_id=fake&target_model=cosyvoice-v3-flash').status_code, 200)
            response = client.post('/api/cloud-voices/register', json={'api_key': 'must-not-echo'})
            self.assertEqual(response.status_code, 422)
            self.assertNotIn('must-not-echo', response.text)
            self.assertEqual(client.post('/api/cloud-voices/register', headers={'Origin': 'https://bad.example'}, json={}).status_code, 403)
            self.assertEqual(client.get('/api/cloud-voices/not-valid/sample').status_code, 404)

    def test_design_and_unknown_voice_fail_configuration(self):
        from web import app as web
        config = legacy_config({})
        config.stages.tts.mode = 'api'; config.stages.tts.provider = 'bailian'
        config.stages.tts.connection_id = 'fake'; config.stages.tts.model = 'cosyvoice-v3-flash'
        config.timing.source = 'tts_timestamp'
        with patch.object(web._MODEL_CONNECTIONS, 'freeze', return_value=({'fake': CONNECTION}, {})), patch.object(web._CLOUD_VOICES, 'get', return_value={'status': 'OK', 'timestamp_status': 'unknown'}):
            with self.assertRaises(ConfigurationError):
                web._configure_job_models({'stages': ['2'], 'review_before_render': True}, {'model_config': config.public_dict(), 'voice': {'kind': 'cloud', 'resource_id': 'fake'}})


if __name__ == '__main__':
    unittest.main(verbosity=2)
