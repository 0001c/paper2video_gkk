"""P7 Web专项离线验证：只有模拟任务，禁止云请求或素材上传。"""
import asyncio
import copy
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
from pydantic import ValidationError
from web import app as web
from web import model_catalog, model_config
from web.model_connections import ConnectionCreate, ConnectionStore


class AvatarWebChecks(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='wan-web-', dir=ROOT / 'jobs/_validation')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        for name, value in [('JOBS_ROOT', str(self.root)), ('_CURRENT', {'job_id': None, 'proc': None}),
                            ('_JOB_KEYS', {}), ('_MODEL_CONNECTIONS', ConnectionStore())]:
            patcher = patch.object(web, name, value)
            patcher.start(); self.addCleanup(patcher.stop)
        self.client = TestClient(web.app, base_url='http://localhost:8848', client=('127.0.0.1', 50000))
        self.config = model_config.legacy_config({'generate_talking': True}).public_dict()
        self.config['avatar'] = {'enabled': True, 'mode': 'api', 'provider': 'bailian', 'model': 'wan2.2-s2v', 'connection_id': 'expired', 'parameters': {'resolution': '480P'}}
        self.job = self.save('job_unit', 'failed')
        self.record = self.write_record(self.job, 'wan-task-0000.json', task_id='remote_123', state='submitted')
        self.missing = self.write_record(self.job, 'wan-task-0001.json', state='submission_unknown')

    def save(self, identity, status, config=None):
        (self.root / identity).mkdir(exist_ok=True)
        job = {'id': identity, 'status': status, 'phase': '失败', 'config': {'model_config_origin': 'v2', 'model_config': copy.deepcopy(config or self.config)}}
        web._save_job(job)
        return job

    def write_record(self, job, name, **fields):
        path = Path(web._result_dir(job), 'talking_wan2.2-s2v/.segments/0/p6id/wan_identity', name)
        path.parent.mkdir(parents=True, exist_ok=True)
        record = {'schema_version': 1, 'provider': 'bailian', 'model': 'wan2.2-s2v', 'parameters': {'resolution': '480P'}, 'request_id': 'request_456',
                  'endpoint': 'https://dashscope.aliyuncs.com/api/v1', 'scope_id': 'unit_scope',
                  'output_path': 'C:/private/output.mp4', 'input_url': 'https://unsafe.example/a?Signature=SECRET', 'api_key': 'SECRET', **fields}
        path.write_text(json.dumps(record), encoding='utf-8')
        return path

    def post(self, suffix, **kwargs):
        return self.client.post('/api/jobs/job_unit/' + suffix, **kwargs)

    def test_wan_configuration_and_catalog(self):
        cfg = model_config.PipelineModelConfig.model_validate(self.config)
        model_config.ensure_implemented(cfg, ['avatar'])
        self.assertEqual(cfg.avatar.parameters.resolution, '480P')
        self.assertTrue(model_config.capabilities()['implemented']['bailian_avatar'])
        for value in ['1080P', '720p', 720]:
            bad = copy.deepcopy(self.config); bad['avatar']['parameters']['resolution'] = value
            with self.assertRaises(ValidationError): model_config.PipelineModelConfig.model_validate(bad)
        local = model_config.legacy_config({'generate_talking': True}).public_dict()
        self.assertEqual(local['avatar']['parameters'], {})
        local['avatar']['parameters'] = {'resolution': '480P'}
        with self.assertRaises(ValidationError): model_config.PipelineModelConfig.model_validate(local)
        item = model_catalog.candidate({'id': 'wan2.2-s2v', 'capabilities': [], 'sources': ['native'], 'offline_time': None}, 'avatar', 'bailian')
        self.assertTrue(item['selectable']); self.assertEqual(item['protocols'], ['bailian_native_async_avatar'])

    def test_job_projection_has_ids_and_manual_review_without_secrets(self):
        response = self.client.get('/api/jobs/job_unit')
        self.assertEqual(response.status_code, 200)
        remote = response.json()['remote_tasks']
        self.assertEqual(remote['known_ids'], ['remote_123'])
        self.assertTrue(remote['needs_manual_review'])
        self.assertEqual(remote['items'][1]['state'], 'needs_manual_review')
        self.assertNotIn('SECRET', response.text); self.assertNotIn('unsafe.example', response.text); self.assertNotIn('C:/private', response.text)

    def test_reconcile_only_calls_known_id(self):
        calls = []
        def reconcile(path):
            calls.append(path)
            record = json.loads(path.read_text()); record.update(state='succeeded', remote_status='SUCCEEDED')
            path.write_text(json.dumps(record))
        with patch.object(web, '_avatar_client', return_value=SimpleNamespace(reconcile=reconcile)):
            response = self.post('remote-tasks/reconcile')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(calls, [self.record]); self.assertFalse(response.json()['submitted']); self.assertFalse(response.json()['resumed'])
        self.assertTrue(response.json()['needs_manual_review']); self.assertNotIn('SECRET', response.text)

    def test_reconcile_missing_id_needs_no_key_or_submit(self):
        self.record.unlink()
        with patch.object(web, '_avatar_client', side_effect=AssertionError('缺ID禁止任何云请求')):
            response = self.post('remote-tasks/reconcile')
        self.assertEqual(response.status_code, 200); self.assertTrue(response.json()['needs_manual_review'])

    def test_reconcile_active_or_shared_directory_is_busy(self):
        for status in ('running', 'queued'):
            self.job['status'] = status; web._save_job(self.job)
            with patch.object(web, '_avatar_client', side_effect=AssertionError('运行中不得远程操作')):
                self.assertEqual(self.post('remote-tasks/reconcile').status_code, 409)
        self.job['status'] = 'failed'; web._save_job(self.job)
        other = self.save('job_other', 'running')
        other['config']['result_dir'] = web._result_dir(self.job); web._save_job(other)
        self.assertEqual(self.post('remote-tasks/reconcile').status_code, 409)

    def test_reconcile_expired_key_then_same_scope_rebind(self):
        self.assertEqual(self.post('remote-tasks/reconcile').status_code, 409)
        store = web._MODEL_CONNECTIONS
        new = store.create(ConnectionCreate(label='离线重绑', provider='bailian', scope_id='unit_scope',
            compatible_base_url='https://dashscope.aliyuncs.com/compatible-mode/v1', native_http_base_url='https://dashscope.aliyuncs.com/api/v1', api_key='unit-key'))
        original = {**new, 'id': 'expired'}
        self.job['config']['model_connections'] = {'expired': original}
        self.job['config']['required_model_roles'] = ['avatar']
        web._save_job(self.job)
        response = self.post('model-connections/rebind', json={'bindings': {'expired': new['id']}})
        self.assertEqual(response.status_code, 200); self.assertFalse(response.json()['resumed'])
        with patch.object(web, '_avatar_client', return_value=SimpleNamespace(reconcile=lambda path: None)):
            self.assertEqual(self.post('remote-tasks/reconcile').status_code, 200)
        self.assertNotIn('unit-key', (self.root / 'job_unit/job.json').read_text())

    def test_cancel_queued_task_does_not_kill_other_process(self):
        local = model_config.legacy_config({'generate_talking': False}).public_dict()
        self.record.unlink(); self.missing.unlink()
        self.job = self.save('job_unit', 'queued', local)
        proc = SimpleNamespace(kill=lambda: self.fail('取消排队任务误杀其他进程'))
        web._CURRENT.update(job_id='job_other', proc=proc)
        response = self.post('cancel')
        self.assertEqual(response.status_code, 200); self.assertTrue(response.json()['ok'])
        self.assertEqual(web._load_job('job_unit')['status'], 'canceled')
        self.assertTrue(web._avatar_cancel_file('job_unit').is_file())

    def test_cancel_persists_marker_and_intent_before_kill(self):
        self.job['status'] = 'running'; web._save_job(self.job)
        def kill():
            self.assertTrue(web._avatar_cancel_file('job_unit').is_file())
            self.assertTrue(web._load_job('job_unit')['cancellation_requested'])
        async def wait(): return 1
        web._CURRENT.update(job_id='job_unit', proc=SimpleNamespace(kill=kill, wait=wait))
        calls = []
        def cancel(path):
            calls.append(path)
            self.assertTrue(web._avatar_cancel_file('job_unit').is_file())
        with patch.object(web, '_avatar_client', return_value=SimpleNamespace(cancel=cancel)):
            response = self.post('cancel')
        self.assertEqual(calls, [self.record]); self.assertEqual(response.json()['remote_cancel_state'], 'cancel_unconfirmed')
        self.assertIn('计费', response.json()['warning'])

    def test_cancel_only_confirmed_when_all_cloud_tasks_confirmed(self):
        self.missing.unlink()
        def cancel(path):
            record = json.loads(path.read_text()); record.update(state='canceled', remote_status='CANCELED', cancellation_confirmed=True, possible_billing=False)
            path.write_text(json.dumps(record))
        with patch.object(web, '_avatar_client', return_value=SimpleNamespace(cancel=cancel)):
            response = self.post('cancel')
        self.assertEqual(response.json()['remote_cancel_state'], 'cancel_confirmed')

    def test_unknown_remote_error_is_actionable_and_redacted(self):
        def fail(path): raise ValueError('https://unsafe.example/?Signature=SECRET')
        with patch.object(web, '_avatar_client', return_value=SimpleNamespace(reconcile=fail)):
            response = self.post('remote-tasks/reconcile')
        self.assertEqual(response.status_code, 200); self.assertFalse(response.json()['ok'])
        self.assertTrue(response.json()['errors']); self.assertNotIn('SECRET', response.text)

    def test_reconcile_body_limit_and_origin_are_inherited(self):
        self.assertEqual(self.post('remote-tasks/reconcile', content=b'x' * 16385).status_code, 413)
        self.assertEqual(self.post('remote-tasks/reconcile', headers={'Origin': 'http://attacker.test'}).status_code, 403)

    def test_worker_preserves_cancel_status_and_warning(self):
        self.job['config'].update(stages=['3'], force_regenerate=True)
        web._save_job(self.job)
        async def wait():
            await web._update_job('job_unit', status='canceled', cancellation_requested=True, remote_cancel_state='cancel_unconfirmed')
            return 1
        async def process(*args, **kwargs):
            self.assertEqual(kwargs['env']['PAPER2VIDEO_AVATAR_CANCEL_FILE'], str(web._avatar_cancel_file('job_unit')))
            return SimpleNamespace(wait=wait)
        self.job['config']['model_config_origin'] = 'legacy'
        with patch.object(web, '_build_argv', return_value=['unit-offline']), patch.object(web, '_artifact_facts', return_value={}), \
             patch.object(web.artifact_cache, 'struct_key', return_value='unit-struct'), patch.object(web.artifact_cache, 'deck_key', return_value='unit-deck'), \
             patch.object(web.asyncio, 'create_subprocess_exec', side_effect=process):
            asyncio.run(web._run_job(self.job))
        final = web._load_job('job_unit')
        self.assertEqual(final['status'], 'canceled'); self.assertIn('计费', final['phase']); self.assertEqual(final['exit_code'], 1)

    def test_provider_failure_states_keep_known_id_recoverable(self):
        for state in ('query_failed', 'download_failed', 'poll_timeout', 'remote_unknown'):
            record = json.loads(self.record.read_text()); record['state'] = state
            self.record.write_text(json.dumps(record))
            item = web._public_remote_record(self.job, self.record)
            self.assertEqual(item['state'], state)
            self.assertEqual(item['needs_manual_review'], state == 'remote_unknown')

    def test_never_submitted_canceled_record_needs_no_cloud_call(self):
        self.record.unlink()
        record = json.loads(self.missing.read_text())
        record.update(state='cancelled_before_submission', cancellation_confirmed=True, possible_billing=False)
        self.missing.write_text(json.dumps(record))
        with patch.object(web, '_avatar_client', side_effect=AssertionError('提交前已停止不需云请求')):
            response = self.post('cancel')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['remote_cancel_state'], 'cancel_confirmed')
        self.assertFalse(response.json()['remote_tasks']['needs_manual_review'])

    def test_wan_confirmation_does_not_confirm_inflight_cloud_tts(self):
        self.missing.unlink()
        self.job['status'] = 'running'
        self.job['config']['model_config']['stages']['tts'].update(mode='api', provider='bailian', connection_id='expired', model='cosyvoice-v3.5-plus')
        web._save_job(self.job)
        def cancel(path):
            record = json.loads(path.read_text()); record.update(state='canceled', remote_status='CANCELED', cancellation_confirmed=True, possible_billing=False)
            path.write_text(json.dumps(record))
        with patch.object(web, '_avatar_client', return_value=SimpleNamespace(cancel=cancel)):
            response = self.post('cancel')
        self.assertEqual(response.json()['remote_cancel_state'], 'cancel_unconfirmed')
        self.assertTrue(web._load_job('job_unit')['other_cloud_cancel_unconfirmed'])

    def test_worker_exception_does_not_override_canceled_intent(self):
        self.job['status'] = 'queued'; web._save_job(self.job)
        async def fail(job):
            await web._update_job(job['id'], status='canceled', phase='云端可能继续计费', remote_cancel_state='cancel_unconfirmed')
            raise RuntimeError('offline failure after cancel')
        async def run():
            queue = asyncio.Queue()
            await queue.put('job_unit')
            with patch.object(web, '_QUEUE', queue), patch.object(web, '_run_job', side_effect=fail):
                worker = asyncio.create_task(web._worker_loop())
                await queue.join()
                worker.cancel()
                with self.assertRaises(asyncio.CancelledError): await worker
        asyncio.run(run())
        final = web._load_job('job_unit')
        self.assertEqual(final['status'], 'canceled'); self.assertIn('计费', final['phase'])

    def test_new_route_obeys_public_authentication_and_origin(self):
        from web.public_access import PublicAccess, password_digest
        origin = 'https://video.example.test:23741'
        salt = '1234567890abcdef1234567890abcdef'
        access = PublicAccess([origin], 'admin', salt, password_digest('offline-password', salt))
        with patch.object(web.app.state, 'public_access', access):
            client = TestClient(web.app, base_url=origin, client=('127.0.0.1', 50000))
            self.assertEqual(client.post('/api/jobs/job_unit/remote-tasks/reconcile', headers={'Origin': origin}).status_code, 401)
            response = client.post('/api/auth/login', json={'username': 'admin', 'password': 'offline-password'}, headers={'Origin': origin})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(client.post('/api/jobs/job_unit/remote-tasks/reconcile').status_code, 403)
            self.assertEqual(client.post('/api/jobs/job_unit/remote-tasks/reconcile', headers={'Origin': origin}).status_code, 409)

    def test_kill_failure_is_not_reported_as_local_stopped(self):
        self.job['status'] = 'running'; web._save_job(self.job)
        def fail(): raise OSError('offline process still running')
        web._CURRENT.update(job_id='job_unit', proc=SimpleNamespace(kill=fail))
        with patch.object(web, '_avatar_client', side_effect=AssertionError('进程未停止不得操作远程记录')):
            response = self.post('cancel')
            self.assertFalse(response.json()['ok'])
            self.assertIn('尚未确认停止', response.json()['warning'])
            self.assertEqual(self.post('remote-tasks/reconcile').status_code, 409)


if __name__ == '__main__':
    unittest.main()
