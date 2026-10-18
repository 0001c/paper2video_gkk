"""教学一键配置回归：隔离素材、替身模型，不发起云请求。"""
import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from fastapi.testclient import TestClient
from web import app as web
from web import course_assistant
from web.model_connections import ConnectionCreate, ConnectionStore


SUGGESTION = {'objectives': '解释梯度含义，并完成一个简单求导练习。',
              'focus': '重点：梯度方向。难点：偏导与整体变化的联系。',
              'depth': '从二元函数的图形出发，结合一步计算例题与随堂练习。'}


class CourseAssistantTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='.course-assist-check-', dir=ROOT)
        self.root = Path(self.temp.name)
        self.patches = [patch.object(web, 'CONTENT_ROOT', str(self.root)),
                        patch.object(web, '_MODEL_CONNECTIONS', ConnectionStore()),
                        patch.object(web, '_MODEL_CATALOG', web.model_catalog.CatalogStore()),
                        patch.object(web, '_COURSE_ASSIST_LOCK', asyncio.Lock())]
        for item in self.patches:
            item.start()
        self.client = TestClient(web.app, client=('127.0.0.1', 50000))
        self.work = self.root / ('a' * 12)
        (self.work / 'ingest').mkdir(parents=True)
        self.source = '# 梯度\n梯度由各个方向的偏导数组成。\n# 范围外内容\n不应传给模型的外部章节。'
        (self.work / 'ingest/paper_text.md').write_text(self.source, encoding='utf-8')
        section = web.course.source_sections(self.source)[1]
        entry = web._MODEL_CONNECTIONS.create(ConnectionCreate(label='离线替身', provider='openai_compatible',
            compatible_base_url='http://127.0.0.1:18000/v1', api_key='offline-test-secret'))
        self.payload = {'ingest_id': 'content_' + 'a' * 12, 'slide_count': 8, 'script_lang': 'zh',
            'settings': {'section_ids': [section['id']], 'audience': '学过一元微积分的大一学生'},
            'text_model': {'mode': 'local', 'provider': 'openai_compatible', 'model': 'offline-model',
                           'connection_id': entry['id'], 'parameters': {'max_output_tokens': 2048, 'temperature': 0.2}}}
        self.mock = patch('openai.OpenAI')
        self.factory = self.mock.start()
        self.create = self.factory.return_value.__enter__.return_value.chat.completions.create
        self.respond(json.dumps(SUGGESTION, ensure_ascii=False))

    def tearDown(self):
        self.mock.stop()
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def respond(self, text, reason='stop'):
        self.create.return_value = SimpleNamespace(usage=None, choices=[SimpleNamespace(
            finish_reason=reason, message=SimpleNamespace(content=text))])

    def post(self, **changes):
        return self.client.post('/api/course/assist', json={**self.payload, **changes})

    def test_only_selected_source_audience_and_model_are_sent(self):
        response = self.post()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), SUGGESTION)
        args = self.create.call_args.kwargs
        prompt = json.loads(args['messages'][1]['content'])
        self.assertEqual(prompt['audience'], self.payload['settings']['audience'])
        self.assertEqual(prompt['slide_count'], 8)
        self.assertIn('梯度', prompt['source'])
        self.assertNotIn('范围外内容', prompt['source'])
        self.assertEqual(args['model'], 'offline-model')
        self.assertEqual(args['max_tokens'], 2048)
        self.assertEqual(args['temperature'], 0.2)
        self.assertEqual(self.factory.call_args.kwargs['max_retries'], 0)
        self.assertNotIn('offline-test-secret', response.text)
        self.assertFalse(list(self.root.rglob('job.json')))
        self.assertEqual((self.work / 'ingest/paper_text.md').read_text(encoding='utf-8'), self.source)
        ledger = json.loads(next(self.work.glob('model_usage/*.json')).read_text(encoding='utf-8'))
        self.assertEqual(ledger['state'], 'succeeded')

    def test_result_can_be_used_for_plan_and_edited(self):
        suggestion = self.post().json()
        for focus in (suggestion['focus'], '手动调整：着重讲解方向导数。'):
            response = self.client.post('/api/course/plan', json={key: value for key, value in {
                **self.payload, 'settings': {**self.payload['settings'], **suggestion, 'focus': focus}
            }.items() if key != 'text_model'})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()['focus'], focus)

    def test_prerequisites_reject_before_model_request(self):
        variants = [({'settings': {'section_ids': ['all'], 'audience': '  '}}, 422),
                    ({'settings': {'section_ids': [], 'audience': '大学生'}}, 422),
                    ({'settings': {'section_ids': ['missing'], 'audience': '大学生'}}, 422),
                    ({'settings': {**self.payload['settings'], 'typo': True}}, 422),
                    ({'text_model': {**self.payload['text_model'], 'connection_id': 'missing'}}, 409),
                    ({'text_model': {**self.payload['text_model'], 'provider': 'bailian'}}, 422),
                    ({'ingest_id': 'content_' + 'b' * 12}, 404), ({'slide_count': 0}, 422),
                    ({'typo': True}, 422)]
        for changes, status in variants:
            with self.subTest(changes=changes):
                self.assertEqual(self.post(**changes).status_code, status)
        self.create.assert_not_called()

    def test_origin_access_and_body_limits(self):
        self.assertEqual(self.client.post('/api/course/assist', json=self.payload,
            headers={'Origin': 'https://untrusted.invalid'}).status_code, 403)
        remote = TestClient(web.app, client=('192.0.2.1', 50000))
        self.assertEqual(remote.post('/api/course/assist', json=self.payload).status_code, 503)
        self.assertEqual(self.client.post('/api/course/assist', content=b'x' * 262145).status_code, 413)
        self.create.assert_not_called()

    def test_invalid_or_truncated_output_is_not_partial_success(self):
        variants = [('not json', 'stop'), (json.dumps({'objectives': 'only one'}), 'stop'),
                    (json.dumps({**SUGGESTION, 'unexpected': True}), 'stop'),
                    (json.dumps({**SUGGESTION, 'focus': '  '}), 'stop'),
                    (json.dumps({**SUGGESTION, 'focus': '长' * 2001}), 'stop'),
                    (json.dumps(SUGGESTION), 'length')]
        for text, reason in variants:
            with self.subTest(reason=reason, text=text[:40]):
                self.respond(text, reason)
                response = self.post()
                self.assertEqual(response.status_code, 502, response.text)
                self.assertNotIn('objectives', response.json())

    def test_fenced_json_is_supported(self):
        self.respond('```json\n' + json.dumps(SUGGESTION) + '\n```')
        self.assertEqual(self.post().json(), SUGGESTION)

    def test_failure_does_not_leak_provider_secrets_or_retry(self):
        self.create.side_effect = RuntimeError('offline-test-secret provider diagnostic')
        response = self.post()
        self.assertEqual(response.status_code, 502)
        self.assertNotIn('offline-test-secret', response.text)
        self.assertEqual(self.create.call_count, 1)
        self.create.side_effect = None
        self.assertEqual(self.post().status_code, 200)

    def test_busy_request_does_not_trigger_second_model_call(self):
        asyncio.run(web._COURSE_ASSIST_LOCK.acquire())
        self.assertEqual(self.post().status_code, 429)
        self.create.assert_not_called()
        web._COURSE_ASSIST_LOCK.release()
        self.assertEqual(self.post().status_code, 200)


if __name__ == '__main__':
    unittest.main(verbosity=2)
