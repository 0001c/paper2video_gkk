"""Wan费用离线回归：仅写临时账本，不调用或上传远程素材。"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from web import model_costs


class AvatarCosts(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def begin(self, model='wan2.2-s2v', endpoint='https://dashscope.aliyuncs.com/api/v1'):
        return model_costs.begin('avatar', model, endpoint, self.root)

    def test_detection_rejected_portrait_still_costs_successful_request(self):
        ticket = self.begin('wan2.2-s2v-detect')
        model_costs.finish(ticket, {'images': 1, 'check_pass': False})
        self.assertEqual(ticket[1]['estimated_cny'], 0.004)
        self.assertEqual(ticket[1]['usage'], {'images': 1})

    def test_output_duration_and_resolution_decimal_prices(self):
        for resolution, duration, expected in ((480, 7.25, 3.625), (720, 12.34, 11.106)):
            ticket = self.begin()
            model_costs.finish(ticket, {'duration_seconds': duration, 'resolution': resolution, 'input_audio_seconds': 19})
            self.assertEqual(ticket[1]['estimated_cny'], expected)
            self.assertEqual(ticket[1]['usage'], {'duration_seconds': duration, 'resolution': resolution})

    def test_missing_invalid_usage_is_unknown(self):
        cases = [{}, {'input_audio_seconds': 18, 'resolution': 480},
                 {'duration_seconds': 2}, {'duration_seconds': 2, 'resolution': '480P'},
                 {'duration_seconds': 2, 'resolution': 1080},
                 *({'duration_seconds': value, 'resolution': 480} for value in (0, -1, True, '2', float('nan'), float('inf'), 10 ** 1000))]
        for usage in cases:
            with self.subTest(usage=usage):
                ticket = self.begin()
                model_costs.finish(ticket, usage)
                self.assertIsNone(ticket[1]['estimated_cny'])
                self.assertNotIn('NaN', json.dumps(ticket[1], allow_nan=False))
        for images in (0, -1, True, 1.2, '1'):
            ticket = self.begin('wan2.2-s2v-detect')
            model_costs.finish(ticket, {'images': images})
            self.assertIsNone(ticket[1]['estimated_cny'])

    def test_non_success_and_unknown_region_do_not_invent_fee(self):
        for state in ('submission_unknown', 'running', 'failed', 'cancel_unconfirmed'):
            ticket = self.begin()
            model_costs.finish(ticket, {'duration_seconds': 18, 'resolution': 480}, state=state)
            self.assertIsNone(ticket[1]['estimated_cny'])
        ticket = self.begin(endpoint='https://dashscope-intl.aliyuncs.com/api/v1')
        model_costs.finish(ticket, {'duration_seconds': 18, 'resolution': 720})
        self.assertIsNone(ticket[1]['estimated_cny'])

    def test_resume_same_request_and_read_only_summary(self):
        ticket = self.begin()
        record_id = ticket[1]['id']
        model_costs.finish(ticket, state='running')
        resumed = model_costs.restore(self.root, record_id)
        self.assertEqual(resumed[1]['id'], record_id)
        model_costs.finish(resumed, {'duration_seconds': 4.125, 'resolution': 720})
        self.assertEqual(len(list((self.root / 'model_usage').glob('*.json'))), 1)
        filename = self.root / 'model_usage' / (record_id + '.json')
        before = filename.read_bytes()
        group = next(item for item in model_costs.summary(self.root)['stages'] if item['role'] == 'avatar')
        self.assertEqual(group['requests'], 1)
        self.assertEqual(group['estimated_cny'], 3.7125)
        self.assertEqual(group['usage'], {'duration_seconds': 4.125})
        self.assertEqual(filename.read_bytes(), before)

    def test_restore_rejects_missing_corrupt_and_path_escape(self):
        for record_id in ('../escape', 'x' * 32, '0' * 32):
            with self.assertRaises(ValueError):
                model_costs.restore(self.root, record_id)
        ticket = self.begin()
        filename = self.root / 'model_usage' / (ticket[1]['id'] + '.json')
        for content in ('{', '[]', '{"id":"wrong"}'):
            filename.write_text(content, encoding='utf-8')
            with self.assertRaises(ValueError):
                model_costs.restore(self.root, ticket[1]['id'])

    def test_old_price_record_is_not_rewritten_by_query(self):
        ticket = model_costs.begin('tts', 'cosyvoice-v3-flash', 'https://dashscope.aliyuncs.com/api/v1', self.root)
        model_costs.finish(ticket, {'characters': 100})
        ticket[1]['price_version'] = '2026-10-03-beijing-standard-v2'
        model_costs.write_record(self.root, ticket[1])
        filename = self.root / 'model_usage' / (ticket[1]['id'] + '.json')
        before = filename.read_bytes()
        report = model_costs.summary(self.root)
        self.assertEqual(report['known_estimated_cny'], 0.01)
        self.assertEqual(filename.read_bytes(), before)
        self.assertEqual(model_costs.restore(self.root, ticket[1]['id'])[1]['price_version'], '2026-10-03-beijing-standard-v2')


if __name__ == '__main__':
    unittest.main(verbosity=2)
