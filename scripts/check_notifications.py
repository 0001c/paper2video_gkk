"""通知与任务结尾分流的离线回归，不向真实 Bark 发送消息。"""
import asyncio
import copy
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from fastapi.testclient import TestClient
from pydantic import ValidationError
from web import app as web, notifications as notify


URL = 'https://api.day.app/TEST_DEVICE_KEY'


class MemoryFile:
    def __init__(self):
        self.records = []
    def save(self, records):
        self.records = copy.deepcopy(records)
    def load(self):
        return copy.deepcopy(self.records)


class NotificationChecks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=ROOT / 'jobs/_validation', prefix='notifications-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = notify.BarkStore(self.root / 'bark.dpapi')
        self.store.storage = MemoryFile()
        self.store.update(notify.BarkUpdate(enabled=True, push_url=URL))

    def test_public_configuration_does_not_reveal_device_key_and_blank_preserves_key(self):
        self.assertNotIn('TEST_DEVICE_KEY', str(self.store.public()))
        self.assertNotIn('push_url', self.store.public())
        self.store.update(notify.BarkUpdate(enabled=False))
        self.assertTrue(self.store.public()['configured'])
        self.store.restore()
        self.assertFalse(self.store.public()['enabled'])
        self.assertEqual(self.store.config.push_url.get_secret_value(), URL)
        self.store.storage.records = [{'push_url': 'malformed'}]
        self.store.restore()
        self.assertIsNotNone(self.store.public()['error'])
        self.assertFalse(self.store.enabled_for('completed'))

    def test_invalid_url_fields_and_disabled_without_address(self):
        for url in ('https://user:password@api.day.app/TEST_DEVICE_KEY', URL + '?x=1', URL + '#x',
                    'http://api.day.app/TEST_DEVICE_KEY', 'http://169.254.169.254/TEST_DEVICE_KEY', 'https://api.day.app/'):
            with self.assertRaises(ValidationError):
                notify.BarkUpdate(push_url=url)
        empty = notify.BarkStore(self.root / 'unused')
        empty.storage = MemoryFile()
        with self.assertRaisesRegex(ValueError, 'push_url'):
            empty.update(notify.BarkUpdate(enabled=True))
        self.assertEqual(notify.destination('https://example.test/bark/TEST_DEVICE_KEY'), ('https://example.test/bark', 'TEST_DEVICE_KEY'))

    def test_post_body_and_transport_error_are_safe(self):
        session = MagicMock()
        session.__enter__.return_value = session
        session.post.return_value.status_code = 200
        session.post.return_value.json.return_value = {'code': 200}
        with patch.object(notify.requests, 'Session', return_value=session):
            self.store.send_job({'id': 'job-example', 'config': {'api_key': 'NEVER_SEND'}, 'qa_error': 'private source'}, 'completed')
        call = session.post.call_args
        self.assertEqual(call.args[0], 'https://api.day.app/push')
        self.assertEqual(call.kwargs['json']['device_key'], 'TEST_DEVICE_KEY')
        self.assertNotIn('NEVER_SEND', str(call.kwargs['json']))
        self.assertNotIn('private source', str(call.kwargs['json']))
        self.assertFalse(call.kwargs['allow_redirects'])
        session.post.return_value.status_code = 500
        with patch.object(notify.requests, 'Session', return_value=session), self.assertRaisesRegex(ValueError, '推送失败') as caught:
            self.store.send('test', 'test')
        self.assertNotIn('TEST_DEVICE_KEY', str(caught.exception))

    def test_events_skip_cancelled_running_and_continuation(self):
        for job in ({'status': 'running'}, {'status': 'queued'}, {'status': 'canceled'}, {'status': 'awaiting_review', 'qa_next_job_id': 'child'}):
            self.assertIsNone(notify.job_event(job))
        for job, event in (({'status': 'done'}, 'completed'), ({'status': 'failed'}, 'failed'),
                           ({'status': 'awaiting_review', 'qa_error': '格式重试用尽'}, 'failed'), ({'status': 'awaiting_review'}, 'review')):
            self.assertEqual(notify.job_event(job), event)

    def test_worker_notification_deduplicates_and_failure_preserves_job(self):
        (self.root / 'original').mkdir()
        with patch.object(web, 'JOBS_ROOT', str(self.root)), patch.object(web, '_BARK', self.store):
            job = {'id': 'original', 'status': 'done', 'phase': '已完成', 'config': {}}
            web._save_job(job)
            with patch.object(self.store, 'send_job') as send:
                asyncio.run(web._notify_job('original'))
                asyncio.run(web._notify_job('original'))
                self.assertEqual(send.call_count, 1)
            self.assertEqual(web._load_job('original')['notification']['status'], 'sent')
            job = web._load_job('original')
            job.update(status='awaiting_review', qa_error='视觉证据重试用尽')
            web._save_job(job)
            with patch.object(self.store, 'send_job', side_effect=ValueError(URL)):
                asyncio.run(web._notify_job('original'))
            actual = web._load_job('original')
            self.assertEqual(actual['status'], 'awaiting_review')
            self.assertEqual(actual['qa_error'], '视觉证据重试用尽')
            self.assertEqual(actual['notification']['status'], 'failed')
            self.assertNotIn('TEST_DEVICE_KEY', str(actual))

    def test_http_save_test_and_validation_never_echo_key(self):
        with patch.object(web, '_BARK', self.store), patch.object(web, '_local_write'), patch.object(self.store, 'send') as send:
            client = TestClient(web.app)
            result = client.put('/api/notifications/bark', json={'enabled': True, 'push_url': URL})
            self.assertEqual(result.status_code, 200)
            self.assertNotIn('TEST_DEVICE_KEY', result.text)
            self.assertEqual(client.post('/api/notifications/bark/test').status_code, 200)
            self.assertEqual(send.call_count, 1)
            result = client.put('/api/notifications/bark', json={'push_url': URL + '?invalid=1'})
            self.assertEqual(result.status_code, 422)
            self.assertIn('push_url', result.text)
            self.assertNotIn('TEST_DEVICE_KEY', result.text)


if __name__ == '__main__':
    unittest.main()
