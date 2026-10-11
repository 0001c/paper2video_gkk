"""本地密钥保存离线回归：真实 Windows DPAPI，仅使用假密钥与临时目录。"""
import json
import asyncio
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from web.credential_storage import CredentialFile, crypt
from web.model_connections import ConnectionCreate, ConnectionUpdate, ConnectionStore
from web.model_config import ConfigurationError, legacy_config


@unittest.skipUnless(os.name == 'nt', '真实 DPAPI 需要 Windows')
class LocalCredentialsChecks(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=ROOT / 'jobs/_validation', prefix='local-credentials-')
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / 'connections.dpapi'
        self.store = ConnectionStore(self.path)

    def create(self, key='offline-test-secret', saved=True, provider='openai_compatible'):
        return self.store.create(ConnectionCreate(label='离线连接', provider=provider, api_key=key,
            compatible_base_url='https://api.deepseek.com/v1' if provider == 'openai_compatible' else 'https://dashscope.aliyuncs.com/compatible-mode/v1', persist_key=saved))

    def reload(self):
        store = ConnectionStore(self.path)
        store.restore()
        self.assertIsNone(store.storage_status()['error'])
        return store

    def test_persist_ciphertext_and_same_account_process_restart(self):
        current = self.create()
        encrypted = self.path.read_bytes()
        self.assertNotIn(b'offline-test-secret', encrypted)
        self.assertNotIn(b'api.deepseek.com', encrypted)
        restored = self.reload()
        self.assertEqual(restored.list()[0]['id'], current['id'])
        self.assertEqual(restored.list()[0]['scope_id'], current['scope_id'])
        self.assertTrue(restored.list()[0]['persist_key'])
        self.assertNotIn('offline-test-secret', json.dumps(restored.list()))
        # 独立进程模拟服务重启，同账户恢复后仍可冻结任务凭据。
        program = "from pathlib import Path; import sys; sys.path.insert(0,sys.argv[1]); from web.model_connections import ConnectionStore; s=ConnectionStore(sys.argv[2]); s.restore(); assert s.storage_status()['error'] is None; assert len(s.list())==1; assert s.list()[0]['key_present']; print('restart PASS')"
        output = subprocess.run([sys.executable, '-c', program, str(ROOT), str(self.path)], capture_output=True, text=True, timeout=15)
        self.assertEqual(output.returncode, 0, output.stderr)
        self.assertEqual(output.stdout.strip(), 'restart PASS')
        config = legacy_config({}, current['id'])
        snapshots, keys = restored.freeze(config, ['text', 'vision'])
        self.assertEqual(keys[current['id']], 'offline-test-secret')
        self.assertNotIn('offline-test-secret', json.dumps(snapshots))

    def test_unsaved_connections_never_enter_file(self):
        temporary = self.create('temporary-memory-key', saved=False)
        self.assertFalse(self.path.exists())
        self.create()
        self.assertNotIn('temporary-memory-key', json.dumps(CredentialFile(self.path).load()))
        self.assertNotIn(temporary['id'], [row['id'] for row in self.reload().list()])

    def test_rotation_and_label_update_preserve_opt_in(self):
        row = self.create()
        self.store.update(row['id'], ConnectionUpdate(api_key='rotated-offline-key'))
        self.store.update(row['id'], ConnectionUpdate(label='新名称'))
        restored = self.reload()
        self.assertEqual(restored._entry(row['id'])['api_key'], 'rotated-offline-key')
        self.assertEqual(restored.list()[0]['label'], '新名称')
        self.assertTrue(restored.list()[0]['persist_key'])

    def test_cancel_storage_removes_disk_copy_keeps_memory_key(self):
        row = self.create()
        self.store.update(row['id'], ConnectionUpdate(persist_key=False))
        self.assertFalse(self.path.exists())
        self.assertTrue(self.store.public(self.store._entry(row['id']))['key_present'])
        self.assertEqual(self.reload().list(), [])

    def test_delete_saved_connection_preserves_other_saved_keys(self):
        first, second = self.create('first-test-key'), self.create('second-test-key')
        self.store.delete(first['id'])
        restored = self.reload()
        self.assertEqual([row['id'] for row in restored.list()], [second['id']])
        self.assertNotIn('first-test-key', json.dumps(CredentialFile(self.path).load()))
        self.store.delete(second['id'])
        self.assertFalse(self.path.exists())

    def test_encrypt_failure_no_plaintext_fallback_or_memory_mutation(self):
        with patch('web.credential_storage.crypt', side_effect=OSError('offline failure')):
            with self.assertRaises(ConfigurationError) as caught:
                self.create()
        self.assertEqual(caught.exception.status, 503)
        self.assertEqual(self.store.list(), [])
        self.assertFalse(self.path.exists())
        self.assertNotIn('offline-test-secret', str(caught.exception))

    def test_failed_rotation_preserves_previous_file_and_memory_key(self):
        row = self.create()
        previous = self.path.read_bytes()
        with patch('web.credential_storage.os.replace', side_effect=PermissionError('offline failure')):
            with self.assertRaises(ConfigurationError):
                self.store.update(row['id'], ConnectionUpdate(api_key='failed-rotation-key'))
        self.assertEqual(self.path.read_bytes(), previous)
        self.assertEqual(self.store._entry(row['id'])['api_key'], 'offline-test-secret')
        self.assertFalse(list(self.path.parent.glob('*.tmp')))

    def test_damaged_file_does_not_restore_or_overwrite(self):
        self.create()
        encrypted = self.path.read_bytes()
        self.path.write_bytes(encrypted[:-20])
        damaged = self.path.read_bytes()
        other = ConnectionStore(self.path)
        other.restore()
        self.assertEqual(other.list(), [])
        self.assertTrue(other.storage_status()['error'])
        with self.assertRaises(ConfigurationError):
            other.create(ConnectionCreate(label='禁止覆盖', provider='openai_compatible', compatible_base_url='https://api.deepseek.com', api_key='new-test-key', persist_key=True))
        self.assertEqual(self.path.read_bytes(), damaged)

    def test_local_anonymous_connection_metadata_can_restore(self):
        row = self.store.create(ConnectionCreate(label='无密钥本机模型', provider='openai_compatible', compatible_base_url='http://localhost:8000/v1', persist_key=True))
        restored = self.reload().list()[0]
        self.assertEqual(restored['id'], row['id'])
        self.assertFalse(restored['key_present'])

    def test_missing_bailian_key_cannot_be_saved_from_placeholder(self):
        row = self.create(provider='bailian', saved=False)
        self.store._entries[row['id']]['api_key'] = ''
        with self.assertRaises(ConfigurationError) as caught:
            self.store.update(row['id'], ConnectionUpdate(persist_key=True))
        self.assertEqual(caught.exception.field, 'api_key')
        self.assertFalse(self.path.exists())

    def test_api_save_restore_cancel_never_echoes_secret(self):
        from fastapi.testclient import TestClient
        from web import app as service
        client = TestClient(service.app, client=('127.0.0.1', 50000))
        with patch.object(service, '_MODEL_CONNECTIONS', self.store):
            response = client.post('/api/model-connections', json={'label': '离线 API 保存', 'provider': 'openai_compatible', 'compatible_base_url': 'https://api.deepseek.com', 'api_key': 'offline-api-key', 'persist_key': True})
            self.assertEqual(response.status_code, 201)
            self.assertNotIn('offline-api-key', response.text)
            row = response.json()
            restored = self.reload()
        with patch.object(service, '_MODEL_CONNECTIONS', restored):
            response = client.get('/api/model-connections')
            self.assertTrue(response.json()['items'][0]['persist_key'])
            self.assertTrue(response.json()['local_storage']['available'])
            self.assertNotIn('offline-api-key', response.text)
            response = client.patch('/api/model-connections/' + row['id'], json={'persist_key': False})
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.json()['key_present'])
            self.assertFalse(self.path.exists())

    def test_app_startup_restores_keys_before_worker(self):
        from web import app as service
        row = self.create()
        fresh = ConnectionStore(self.path)
        async def worker():
            self.assertTrue(fresh.public(fresh._entry(row['id']))['key_present'])
        async def start():
            await service._startup()
            await service._WORKER
        with patch.object(service, '_MODEL_CONNECTIONS', fresh), patch.object(service, '_worker_loop', worker), patch.object(service, '_QUEUE', None), patch.object(service, '_WORKER', None):
            asyncio.run(start())
        self.assertEqual(fresh.list()[0]['id'], row['id'])


class MemoryOnlyChecks(unittest.TestCase):
    def test_unconfigured_storage_refuses_persist_and_retains_memory_mode(self):
        store = ConnectionStore()
        payload = ConnectionCreate(label='离线测试', provider='openai_compatible', compatible_base_url='http://localhost:8000/v1', api_key='only-memory', persist_key=True)
        with self.assertRaises(ConfigurationError):
            store.create(payload)
        self.assertEqual(store.list(), [])
        payload.persist_key = False
        self.assertFalse(store.create(payload)['persist_key'])


if __name__ == '__main__':
    unittest.main()
