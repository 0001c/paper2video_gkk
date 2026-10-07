"""P7 离线 Wan 协议、防重提交、恢复、取消与费用检查；仅 MockTransport。"""

import json
import subprocess
import sys
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'Paper2Video-main' / 'src'))

import httpx
from PIL import Image
import bailian_avatar as avatar
from bailian_speech import atomic_json
from web import model_costs


CONNECTION = {'scope_id': 'offline-space', 'native_http_base_url': 'https://offline.cn-beijing.maas.aliyuncs.com/api/v1'}
POLICY = {'upload_host': 'https://dashscope-file-test.oss-cn-beijing.aliyuncs.com',
          'upload_dir': 'dashscope-instant/offline/test', 'policy': 'offline-policy-secret',
          'signature': 'offline-signature-secret', 'oss_access_key_id': 'offline-access-key',
          'x_oss_object_acl': 'private', 'x_oss_forbid_overwrite': 'true', 'max_file_size_mb': 100}
RESULT_URL = 'http://dashscope-result-hz.oss-cn-hangzhou.aliyuncs.com/test.mp4?Expires=999&Signature=offline-result-secret'


class WanTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.video_directory = tempfile.TemporaryDirectory()
        filename = Path(cls.video_directory.name) / 'offline.mp4'
        subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y', '-f', 'lavfi', '-i',
                        'color=c=black:s=400x400:d=1:r=25', '-an', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', str(filename)],
                       check=True, capture_output=True, timeout=30)
        cls.video = filename.read_bytes()

    @classmethod
    def tearDownClass(cls):
        cls.video_directory.cleanup()

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.reference = self.root / 'portrait.png'
        Image.new('RGB', (400, 400), 'white').save(self.reference)
        self.audio = self.root / 'segment.wav'
        self.write_audio(1)
        self.record = self.root / 'segment' / 'wan-task-0000.json'
        self.output = self.record.parent / '0000.mp4'
        self.detection = self.root / 'detection.json'
        self.requests = []
        self.task_status = 'SUCCEEDED'
        self.detect_pass = True
        self.usage = {'duration': 1.25, 'SR': 720}
        self.download_body = self.video
        self.cancel_next_status = 'CANCELED'
        self.transport_client = httpx.Client(transport=httpx.MockTransport(self.handler))
        self.client = avatar.AvatarClient(CONNECTION, 'offline-api-secret', self.transport_client, self.root)

    def tearDown(self):
        self.transport_client.close()
        self.directory.cleanup()

    def write_audio(self, seconds):
        with wave.open(str(self.audio), 'wb') as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(24000)
            handle.writeframes(bytes(int(seconds * 24000) * 2))

    def handler(self, request):
        self.requests.append(request)
        if request.url.host.startswith('dashscope-file'):
            self.assertNotIn('authorization', request.headers)
            self.assertNotIn('cookie', request.headers)
            self.assertIn(b'"OSSAccessKeyId"', request.content)
            return httpx.Response(200)
        if request.url.host.startswith('dashscope-result'):
            self.assertEqual(request.url.scheme, 'https')
            self.assertNotIn('authorization', request.headers)
            return httpx.Response(200, content=self.download_body)
        self.assertEqual(request.headers['Authorization'], 'Bearer offline-api-secret')
        if request.url.path.endswith('/uploads'):
            self.assertEqual(str(request.url).split('?')[0], avatar.UPLOAD_POLICY_URL)
            self.assertEqual(request.url.params['action'], 'getPolicy')
            return httpx.Response(200, json={'data': POLICY})
        if request.url.path.endswith('/face-detect'):
            self.assertEqual(request.headers['X-DashScope-OssResourceResolve'], 'enable')
            self.assertNotIn('X-DashScope-Async', request.headers)
            self.assertEqual(json.loads(request.content)['model'], avatar.DETECT_MODEL)
            return httpx.Response(200, json={'output': {'check_pass': self.detect_pass}, 'usage': {'image_count': 1}, 'request_id': 'detect-request'})
        if request.url.path.endswith('/video-synthesis'):
            self.assertEqual(request.headers['X-DashScope-Async'], 'enable')
            self.assertEqual(request.headers['X-DashScope-OssResourceResolve'], 'enable')
            self.assertEqual(json.loads(request.content)['parameters'], {'resolution': '480P'})
            record = json.loads(self.record.read_text(encoding='utf-8'))
            self.assertEqual(record['state'], 'submitting')
            self.assertIsNotNone(record['usage_record_id'])
            return httpx.Response(200, json={'output': {'task_id': 'offline-task', 'task_status': 'PENDING'}, 'request_id': 'submit-request'})
        if request.url.path.endswith('/cancel'):
            self.task_status = self.cancel_next_status
            return httpx.Response(200, json={'request_id': 'cancel-request'})
        if request.url.path.endswith('/tasks/offline-task'):
            saved = json.loads(self.record.read_text(encoding='utf-8'))
            self.assertEqual(saved['task_id'], 'offline-task')
            return httpx.Response(200, json={'output': {'task_id': 'offline-task', 'task_status': self.task_status,
                                                        'results': {'video_url': RESULT_URL}}, 'usage': self.usage, 'request_id': 'query-request'})
        raise AssertionError('未预期请求：' + str(request.url))

    def run_segment(self, **kwargs):
        return self.client.run_segment(self.reference, self.audio, self.record, self.output,
                                       detection_path=self.detection, poll_interval=0.001, timeout=0.001, **kwargs)

    def known_pending(self):
        self.task_status = 'PENDING'
        with self.assertRaisesRegex(ValueError, '轮询超时'):
            self.run_segment()
        self.requests.clear()

    def test_success_protocol_model_bound_uploads_and_output_billing(self):
        result = self.run_segment()
        self.assertEqual(result['state'], 'succeeded')
        self.assertEqual(result['usage'], {'duration_seconds': 1.25, 'resolution': 720})
        self.assertEqual(self.output.read_bytes(), self.video)
        models = [request.url.params['model'] for request in self.requests if request.url.path.endswith('/uploads')]
        self.assertEqual(models, [avatar.DETECT_MODEL, avatar.MODEL, avatar.MODEL])
        self.assertAlmostEqual(model_costs.summary(self.root)['known_estimated_cny'], 0.004 + 1.25 * 0.9)
        for filename in self.root.rglob('*.json'):
            body = filename.read_text(encoding='utf-8')
            for secret in ('offline-api-secret', 'offline-policy-secret', 'offline-signature-secret', 'offline-result-secret', 'oss://'):
                self.assertNotIn(secret, body)

    def test_success_cache_no_http_and_same_ledger(self):
        result = self.run_segment()
        usage_id = result['usage_record_id']
        self.requests.clear()
        result = self.run_segment()
        self.client.reconcile(self.record)
        self.assertEqual(result['usage_record_id'], usage_id)
        self.assertEqual(self.requests, [])
        self.assertEqual(len(list((self.root / 'model_usage').glob('*.json'))), 2)

    def test_known_id_resumes_without_upload_or_submit(self):
        self.known_pending()
        self.task_status = 'SUCCEEDED'
        result = self.run_segment()
        self.assertEqual(result['state'], 'succeeded')
        self.assertTrue(all(request.method == 'GET' for request in self.requests))
        self.assertEqual(len(list((self.root / 'model_usage').glob('*.json'))), 2)

    def test_unknown_submission_blocks_post_after_restart(self):
        original = self.handler
        def handler(request):
            if request.url.path.endswith('/video-synthesis'):
                self.requests.append(request)
                raise httpx.ReadTimeout('offline', request=request)
            return original(request)
        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            self.client.client = client
            with self.assertRaises(ValueError):
                self.run_segment()
        saved = json.loads(self.record.read_text(encoding='utf-8'))
        self.assertEqual(saved['state'], 'submission_unknown')
        self.assertIsNone(saved['task_id'])
        self.requests.clear()
        self.client.client = self.transport_client
        with self.assertRaisesRegex(ValueError, '禁止自动重提'):
            self.run_segment()
        self.assertEqual(self.requests, [])

    def test_detection_reject_still_bills_one_image(self):
        self.detect_pass = False
        with self.assertRaises(ValueError):
            self.run_segment()
        self.assertEqual(model_costs.summary(self.root)['known_estimated_cny'], 0.004)
        self.assertFalse(any(request.url.path.endswith('/video-synthesis') for request in self.requests))
        self.requests.clear()
        with self.assertRaises(ValueError):
            self.run_segment()
        self.assertEqual(self.requests, [])

    def test_detection_missing_usage_counts_submitted_one_image(self):
        original = self.handler
        def handler(request):
            if request.url.path.endswith('/face-detect'):
                self.requests.append(request)
                return httpx.Response(200, json={'output': {'check_pass': False}})
            return original(request)
        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            self.client.client = client
            with self.assertRaises(ValueError):
                self.run_segment()
        self.assertEqual(model_costs.summary(self.root)['known_estimated_cny'], 0.004)

    def test_pending_cancel_only_confirmed_by_get(self):
        self.known_pending()
        result = self.client.cancel(self.record)
        self.assertEqual(result['state'], 'canceled')
        self.assertTrue(result['cancellation_confirmed'])
        self.assertEqual([request.method for request in self.requests], ['GET', 'POST', 'GET'])
        self.assertEqual(result['task_id'], 'offline-task')

    def test_cancel_accepted_but_running_is_unconfirmed_no_repeat_post(self):
        self.known_pending()
        self.cancel_next_status = 'RUNNING'
        result = self.client.cancel(self.record)
        self.assertEqual(result['state'], 'cancel_unconfirmed')
        self.assertTrue(result['possible_billing'])
        self.assertFalse(result['cancellation_confirmed'])
        self.requests.clear()
        self.client.cancel(self.record)
        self.assertEqual([request.method for request in self.requests], ['GET'])

    def test_running_cancel_does_not_post(self):
        self.known_pending()
        self.task_status = 'RUNNING'
        result = self.client.cancel(self.record)
        self.assertEqual(result['state'], 'cancel_unconfirmed')
        self.assertEqual([request.method for request in self.requests], ['GET'])

    def test_cancel_before_submission_no_http(self):
        result = self.run_segment(cancelled=lambda: True)
        self.assertEqual(result['state'], 'cancelled_before_submission')
        self.assertFalse(result['possible_billing'])
        self.assertEqual(self.requests, [])

    def test_exact_20_seconds_and_15_mib_rejected_without_http(self):
        self.write_audio(20)
        with self.assertRaisesRegex(ValueError, '严格小于20秒'):
            self.run_segment()
        with self.audio.open('r+b') as handle:
            handle.truncate(avatar.MAX_AUDIO_BYTES)
        with self.assertRaisesRegex(ValueError, '小于 15 MB'):
            self.run_segment()
        self.assertEqual(self.requests, [])

    def test_download_failure_keeps_output_usage_and_recovers_same_ticket(self):
        self.download_body = b'not-video'
        with self.assertRaisesRegex(ValueError, '归档失败'):
            self.run_segment()
        saved = json.loads(self.record.read_text(encoding='utf-8'))
        self.assertEqual(saved['state'], 'download_failed')
        self.assertEqual(saved['remote_status'], 'SUCCEEDED')
        self.assertAlmostEqual(model_costs.summary(self.root)['known_estimated_cny'], 1.129)
        self.download_body = self.video
        self.requests.clear()
        self.assertEqual(self.client.reconcile(self.record)['state'], 'succeeded')
        self.assertTrue(all(request.method == 'GET' for request in self.requests))
        self.assertEqual(len(list((self.root / 'model_usage').glob('*.json'))), 2)

    def test_fake_ftyp_corrupt_video_not_cached(self):
        self.download_body = b'\x00\x00\x00\x18ftypisom' + bytes(20)
        with self.assertRaisesRegex(ValueError, '归档失败'):
            self.run_segment()
        self.assertFalse(self.output.exists())

    def test_untrusted_hosts_redirects_and_default_headers_never_leak(self):
        for url in ('https://evil.example/test.mp4', 'https://dashscope-result-hz.oss-cn-hangzhou.aliyuncs.com.evil.example/x',
                    'https://user:pass@dashscope-result-hz.oss-cn-hangzhou.aliyuncs.com/x'):
            with self.assertRaises(ValueError):
                avatar.oss_url(url)
        original = self.handler
        def handler(request):
            if request.url.host.startswith('dashscope-result'):
                self.requests.append(request)
                self.assertNotIn('authorization', request.headers)
                self.assertNotIn('cookie', request.headers)
                return httpx.Response(302, headers={'Location': 'https://evil.example/video'})
            return original(request)
        with httpx.Client(transport=httpx.MockTransport(handler), headers={'Cookie': 'bad-cookie'}, auth=('user', 'pass')) as client:
            self.client.client = client
            with self.assertRaises(ValueError):
                self.run_segment()
        self.assertFalse(any(request.url.host == 'evil.example' for request in self.requests))

    def test_missing_or_invalid_output_usage_stays_unknown(self):
        self.usage = {'duration': 10 ** 1000, 'SR': True}
        result = self.run_segment()
        self.assertEqual(result['usage'], {})
        ticket = model_costs.restore(self.root, result['usage_record_id'])
        self.assertIsNone(ticket[1]['estimated_cny'])

    def test_scope_change_and_no_id_reconcile_block_without_http(self):
        self.known_pending()
        other = avatar.AvatarClient({**CONNECTION, 'scope_id': 'other-space'}, 'offline-api-secret', self.transport_client, self.root)
        with self.assertRaisesRegex(ValueError, '连接范围'):
            other.reconcile(self.record)
        saved = json.loads(self.record.read_text(encoding='utf-8'))
        saved['task_id'] = None
        saved['state'] = 'submission_unknown'
        atomic_json(self.record, saved)
        with self.assertRaisesRegex(ValueError, '人工核对'):
            self.client.reconcile(self.record)
        self.assertEqual(self.requests, [])

    def test_unknown_remote_never_resubmits(self):
        self.known_pending()
        self.task_status = 'UNKNOWN'
        with self.assertRaisesRegex(ValueError, '无法查询'):
            self.run_segment()
        self.assertTrue(all(request.method == 'GET' for request in self.requests))
        self.assertEqual(json.loads(self.record.read_text(encoding='utf-8'))['state'], 'remote_unknown')

    def test_detection_cache_repairs_ledger_and_never_duplicates(self):
        self.run_segment()
        detection = json.loads(self.detection.read_text(encoding='utf-8'))
        ticket = model_costs.restore(self.root, detection['usage_record_id'])
        model_costs.finish(ticket, state='submission_unknown')
        self.requests.clear()
        self.client._detect(self.reference, self.detection, detection['reference_sha256'], lambda: False)
        self.assertEqual(self.requests, [])
        self.assertEqual(model_costs.restore(self.root, detection['usage_record_id'])[1]['estimated_cny'], 0.004)

    def test_rejected_detection_cache_also_repairs_original_ledger(self):
        self.detect_pass = False
        with self.assertRaises(ValueError):
            self.run_segment()
        detection = json.loads(self.detection.read_text(encoding='utf-8'))
        model_costs.finish(model_costs.restore(self.root, detection['usage_record_id']), state='submission_unknown')
        self.requests.clear()
        with self.assertRaises(ValueError):
            self.run_segment()
        self.assertEqual(self.requests, [])
        self.assertEqual(model_costs.restore(self.root, detection['usage_record_id'])[1]['estimated_cny'], 0.004)

    def test_repeat_success_missing_usage_preserves_known_price_and_duration(self):
        result = self.run_segment()
        self.output.unlink()
        self.usage = {}
        with patch.dict(model_costs.AVATAR_VIDEO_PRICES, {720: '9'}):
            result = self.client.reconcile(self.record)
        self.assertEqual(result['usage'], {'duration_seconds': 1.25, 'resolution': 720})
        self.assertAlmostEqual(model_costs.restore(self.root, result['usage_record_id'])[1]['estimated_cny'], 1.125)

    def test_success_cancel_keeps_cached_video_and_fee_without_http(self):
        result = self.run_segment()
        self.requests.clear()
        result = self.client.cancel(self.record)
        self.assertEqual(result['state'], 'succeeded')
        self.assertTrue(result['cancellation_requested'])
        self.assertEqual(self.requests, [])
        self.assertAlmostEqual(model_costs.summary(self.root)['known_estimated_cny'], 1.129)

    def test_non_string_query_status_persists_query_failed(self):
        self.known_pending()
        self.task_status = []
        with self.assertRaisesRegex(ValueError, '续查失败'):
            self.client.reconcile(self.record)
        self.assertEqual(json.loads(self.record.read_text(encoding='utf-8'))['state'], 'query_failed')
        self.assertTrue(all(request.method == 'GET' for request in self.requests))

    def test_mislabelled_or_animated_portrait_rejected_without_http(self):
        Image.new('RGB', (400, 400)).save(self.reference, format='GIF')
        with self.assertRaisesRegex(ValueError, '单帧静态图'):
            self.run_segment()
        Image.new('RGB', (400, 400), 'red').save(self.reference, format='PNG', save_all=True,
                                               append_images=[Image.new('RGB', (400, 400), 'blue')])
        with self.assertRaisesRegex(ValueError, '单帧静态图'):
            self.run_segment()
        self.assertEqual(self.requests, [])

    def test_record_lock_prevents_concurrent_submits(self):
        with avatar.record_lock(self.record):
            with self.assertRaisesRegex(ValueError, '正在执行'):
                self.run_segment()
        self.assertEqual(self.requests, [])

    def test_download_size_limit_preserves_remote_id(self):
        with patch.object(avatar, 'MAX_VIDEO_BYTES', len(self.video) - 1):
            with self.assertRaisesRegex(ValueError, '归档失败'):
                self.run_segment()
        self.assertEqual(json.loads(self.record.read_text(encoding='utf-8'))['task_id'], 'offline-task')
        self.assertFalse(self.output.exists())


if __name__ == '__main__':
    unittest.main()
