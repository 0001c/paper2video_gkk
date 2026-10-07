"""P7 流水线冒烟：合成 PCM、模拟 Wan；不上传或调用付费模型。"""

import json
import os
import sys
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'Paper2Video-main' / 'src')]

from PIL import Image
import avatar_segments
import bailian_avatar
import talking_gen


class Checks(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.audio = self.root / 'page.wav'
        with wave.open(str(self.audio), 'wb') as handle:
            handle.setparams((1, 2, 1000, 45000, 'NONE', 'not compressed'))
            handle.writeframes(bytes(90000))
        self.reference = self.root / 'portrait.png'
        Image.new('RGB', (64, 64), 'blue').save(self.reference)
        self.snapshot = {'model_config_origin': 'v2', 'model_config': {'avatar': {
            'enabled': True, 'mode': 'api', 'provider': 'bailian', 'model': 'wan2.2-s2v',
            'parameters': {'resolution': '480P'}, 'connection_id': 'wan'}},
            'model_connections': {'wan': {'provider': 'bailian', 'scope_id': 'test-account',
                'native_http_base_url': 'https://dashscope.aliyuncs.com/api/v1'}}}
        self.environment = patch.dict(os.environ, {'PAPER2VIDEO_ROLE_AVATAR_API_KEY': 'fake-key',
            'PAPER2VIDEO_AVATAR_CANCEL_FILE': str(self.root / 'cancel.json')})
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def run_pipeline(self):
        return talking_gen.talking_gen_per_slide('wan2.2-s2v', [(str(self.reference), str(self.audio))],
            str(self.root / 'talking'), [], 'unused', model_snapshot=self.snapshot)

    def test_short_segment_dispatch_and_single_master_audio_cache(self):
        seen = []

        def generate(reference, audio, record_path, output, **kwargs):
            seen.append(avatar_segments.verify_segment(audio))
            self.assertEqual(kwargs['scope_id'], 'test-account')
            self.assertEqual(kwargs['resolution'], '480P')
            self.assertFalse(kwargs['cancelled']())
            Path(output).write_bytes(b'mocked video')
            Path(record_path).write_text(json.dumps({'task_id': 'known-id', 'state': 'succeeded'}))

        def stitch(videos, manifest, master_audio, output):
            self.assertEqual(master_audio, str(self.audio))
            self.assertEqual(len(videos), 3)
            Path(output).write_bytes(b'mocked merged video')

        with patch.object(bailian_avatar, 'AvatarClient') as client, patch.object(avatar_segments, 'stitch', side_effect=stitch) as merger, \
                patch.object(talking_gen, 'resolve_paths', side_effect=AssertionError('本地模型禁止加载')):
            client.return_value.run_segment.side_effect = generate
            result = self.run_pipeline()
            self.run_pipeline()
            self.assertEqual(merger.call_count, 1)
            self.assertEqual(seen, [18, 18, 9] * 2)
            self.assertTrue(result[0][1])
            self.snapshot['model_config']['avatar']['parameters']['resolution'] = '720P'
            client.return_value.run_segment.side_effect = lambda reference, audio, record_path, output, **kwargs: Path(output).write_bytes(b'720p')
            self.run_pipeline()
            self.assertEqual(merger.call_count, 2)
        manifest = json.loads((self.root / 'talking' / '0' / 'portrait' / 'avatar_segments.json').read_text(encoding='utf-8'))
        self.assertEqual(manifest['sample_count'], 45000)
        self.assertEqual(sum(part['sample_count'] for part in manifest['segments']), 45000)
        self.assertEqual(self.audio.stat().st_size, 90044)

    def test_cancel_prevents_new_segment_and_keeps_known_ids(self):
        def generate(reference, audio, record_path, output, **kwargs):
            Path(record_path).write_text(json.dumps({'task_id': 'accepted-id', 'state': 'cancel_unconfirmed'}))
            (self.root / 'cancel.json').write_text('{}')
            raise ValueError('云端任务可能继续计费')

        with patch.object(bailian_avatar, 'AvatarClient') as client, patch.object(avatar_segments, 'stitch') as merger:
            client.return_value.run_segment.side_effect = generate
            with self.assertRaises(ValueError):
                self.run_pipeline()
            self.assertEqual(client.return_value.run_segment.call_count, 1)
            merger.assert_not_called()
        manifest = json.loads((self.root / 'talking' / '0' / 'portrait' / 'avatar_segments.json').read_text(encoding='utf-8'))
        self.assertEqual(manifest['segments'][0]['task_id'], 'accepted-id')
        self.assertEqual(manifest['segments'][0]['state'], 'cancel_unconfirmed')
        with patch.object(bailian_avatar, 'AvatarClient') as client, self.assertRaises(ValueError):
            self.run_pipeline()
        client.return_value.run_segment.assert_not_called()

    def test_disabled_does_not_read_materials(self):
        self.snapshot['model_config']['avatar']['enabled'] = False
        with patch.object(talking_gen, '_wan_per_slide', side_effect=AssertionError('不应执行数字人')):
            self.assertEqual(self.run_pipeline(), [])

    def test_failed_resume_manifest_preserves_later_ids_and_original_error(self):
        def generate(reference, audio, record_path, output, **kwargs):
            Path(record_path).write_text('broken json')
            Path(record_path).with_name('wan-task-0002.json').write_text(json.dumps({
                'task_id': 'later-known-id', 'state': 'running'}))
            raise ValueError('原始恢复失败')

        with patch.object(bailian_avatar, 'AvatarClient') as client:
            client.return_value.run_segment.side_effect = generate
            with self.assertRaisesRegex(ValueError, '原始恢复失败'):
                self.run_pipeline()
        manifest = json.loads((self.root / 'talking' / '0' / 'portrait' / 'avatar_segments.json').read_text(encoding='utf-8'))
        self.assertEqual(manifest['segments'][0]['state'], 'record_invalid')
        self.assertEqual(manifest['segments'][2]['task_id'], 'later-known-id')

    def test_missing_credentials_and_mismatch_fail_closed(self):
        with patch.dict(os.environ, {'PAPER2VIDEO_ROLE_AVATAR_API_KEY': ''}), self.assertRaises(ValueError):
            self.run_pipeline()
        self.snapshot['model_config']['avatar']['model'] = 'other'
        with self.assertRaises(ValueError):
            self.run_pipeline()


if __name__ == '__main__':
    unittest.main(verbosity=2)
