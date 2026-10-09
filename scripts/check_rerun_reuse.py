"""Offline continuation contract and regeneration isolation. No worker or model calls."""
import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'Paper2Video-main/src')]
from fastapi import HTTPException
from web import app as web


class RerunChecks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=ROOT / 'jobs/_validation', prefix='rerun-reuse-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for name, value in [('JOBS_ROOT', str(self.root)), ('_QUEUE', asyncio.Queue()), ('_JOB_KEYS', {})]:
            override = patch.object(web, name, value)
            override.start()
            self.addCleanup(override.stop)
        override = patch.object(web, '_configure_job_models', return_value={})
        self.configure = override.start()
        self.addCleanup(override.stop)
        self.result = self.root / 'original/result'
        (self.result / 'slide_imgs').mkdir(parents=True)
        (self.result / 'slide_imgs/1.png').write_bytes(b'slide')
        (self.result / 'audio').mkdir()
        (self.result / 'audio/0.wav').write_bytes(b'original audio')
        self.script = self.result / 'subtitle_w_cursor.txt'
        self.script.write_text('Original narration | no', encoding='utf-8')
        web._save_job({'id': 'original', 'status': 'failed', 'config': {'result_dir': str(self.result),
            'slide_lang': 'zh', 'script_lang': 'zh', 'generate_talking': False}, 'needs_struct': False})

    def rerun(self, **payload):
        return asyncio.run(web.api_rerun_job('original', payload))

    def test_continue_requires_valid_script_and_never_falls_back_to_generation(self):
        result = self.rerun(action='continue')
        config = web._load_job(result['job_id'])['config']
        self.assertTrue(config['reuse_script'])
        self.assertTrue(config['require_reuse_script'])
        self.assertEqual(config['result_dir'], str(self.result))
        self.assertFalse(config['review_before_render'])
        web._QUEUE.get_nowait()
        child = web._load_job(result['job_id'])
        child['status'] = 'failed'
        web._save_job(child)
        self.script.write_text('broken script', encoding='utf-8')
        with self.assertRaises(HTTPException) as error:
            self.rerun(action='continue')
        self.assertEqual(error.exception.status_code, 409)

    def test_ambiguous_regeneration_is_rejected_before_any_model_call(self):
        for payload in ({'reuse_script': False}, {'action': 'continue', 'reuse_script': False}):
            with self.assertRaises(HTTPException) as error:
                self.rerun(**payload)
            self.assertEqual(error.exception.status_code, 422)
        self.configure.assert_not_called()
        self.assertEqual(self.script.read_text('utf-8'), 'Original narration | no')

    def test_explicit_regeneration_isolates_script_audio_and_selected_review(self):
        result = self.rerun(action='regenerate_script', reuse_script=False, review_script='Edited narration | no', review_pages=[0])
        new = web._load_job(result['job_id'])
        branch = Path(new['config']['result_dir'])
        self.assertNotEqual(branch, self.result)
        self.assertEqual((branch / 'subtitle_w_cursor.txt').read_text('utf-8'), 'Edited narration | no')
        self.assertEqual((branch / 'audio/0.wav').read_bytes(), b'original audio')
        (branch / 'audio/0.wav').write_bytes(b'new audio')
        self.assertEqual((self.result / 'audio/0.wav').read_bytes(), b'original audio')
        self.assertEqual(self.script.read_text('utf-8'), 'Original narration | no')
        self.assertEqual(new['config']['regenerate_pages'], [0])

    def test_preflight_token_rejects_script_changes(self):
        token = web._rerun_plan(web._load_job('original'))['token']
        self.script.write_text('New narration | no', encoding='utf-8')
        with self.assertRaises(HTTPException) as error:
            self.rerun(plan_token=token, action='continue', reuse_script=True)
        self.assertEqual(error.exception.status_code, 409)
        self.configure.assert_not_called()

    def test_review_rewrite_does_not_save_edits_into_original_result(self):
        payload = web.ReviewRunRequest(version='0' * 64, pages=['Edited narration | no'], action='regenerate', page_ids=[0])
        with patch.object(web, '_local_write'), patch.object(web, '_check_review', return_value='Edited narration | no'):
            result = asyncio.run(web.api_run_review('original', payload, None))
        branch = Path(web._load_job(result['job_id'])['config']['result_dir'])
        self.assertEqual(self.script.read_text('utf-8'), 'Original narration | no')
        self.assertEqual((branch / 'subtitle_w_cursor.txt').read_text('utf-8'), 'Edited narration | no')


if __name__ == '__main__':
    unittest.main()
