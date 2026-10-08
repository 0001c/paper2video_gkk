import sys
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from web.model_catalog import CatalogRequest, CatalogStore, SNAPSHOT, candidate, fetch_directories, merged
from web.model_config import ConfigurationError, legacy_config


class CatalogChecks(unittest.TestCase):
    def setUp(self):
        self.entry = {'id': 'unit-connection', 'provider': 'bailian', 'compatible_base_url': SNAPSHOT['base_url']}
        self.store = CatalogStore()

    def test_union_keeps_native_only_and_compatible_only(self):
        items = merged(['text', 'shared'], [{'model': 'shared', 'capabilities': ['VU']}, {'model': 'voice', 'capabilities': ['TTS']}])
        self.assertEqual({item['id'] for item in items}, {'text', 'shared', 'voice'})
        self.assertEqual(next(item for item in items if item['id'] == 'shared')['sources'], ['native', 'compatible'])
        voice = next(item for item in items if item['id'] == 'voice')
        self.assertIsNone(candidate(voice, 'text', 'bailian'))
        self.assertFalse(candidate(voice, 'tts', 'bailian')['selectable'])

    def test_role_protocol_and_lifecycle(self):
        record = {'id': 'unit', 'capabilities': ['TG'], 'sources': ['native', 'compatible'], 'offline_time': '2026-10-10 00:00:00'}
        self.assertIsNone(candidate(record, 'vision', 'bailian'))
        self.assertEqual(candidate(record, 'text', 'bailian', date(2026, 10, 3))['status'], 'retiring')
        self.assertFalse(candidate(record, 'text', 'bailian', date(2026, 10, 10))['selectable'])
        record['offline_time'] = 'unknown'
        self.assertFalse(candidate(record, 'text', 'bailian')['selectable'])

    def test_snapshot_pagination_and_connection_bound_cursor(self):
        request = CatalogRequest(connection_id=self.entry['id'], role='text', limit=3)
        result = self.store.query(self.entry, request)
        self.assertEqual(result['source'], 'workspace_snapshot')
        self.assertTrue(result['next_cursor'])
        request.cursor = result['next_cursor']
        page = self.store.query(self.entry, request)
        self.assertFalse({item['id'] for item in result['items']} & {item['id'] for item in page['items']})
        for changes in ({'role': 'vision'}, {'limit': 4}, {'connection_id': 'other'}, {'include_retiring': True}, {'refresh': True}):
            with self.assertRaises(ConfigurationError):
                self.store.query({**self.entry, **({'id': 'other'} if 'connection_id' in changes else {})}, request.model_copy(update=changes))
        self.assertIsNone(self.store.snapshot({**self.entry, 'compatible_base_url': 'https://dashscope.aliyuncs.com/compatible-mode/v1'}))

    def test_refresh_completes_all_pages_without_500_truncation(self):
        calls = []
        def get(url, params):
            calls.append((url, params))
            if '/compatible-mode/' in url:
                return {'data': [{'id': 'shared' if params else 'first'}], 'has_more': not bool(params)}
            page = params['page_no']
            count = 100 if page < 6 else 18
            return {'data': {'total': 518, 'models': [{'model': f'native-{(page - 1) * 100 + index}', 'capabilities': ['TTS']} for index in range(count)]}}
        rows = fetch_directories(self.entry, get)
        self.assertEqual(len(rows), 520)
        self.assertEqual(len(calls), 8)
        self.assertTrue(all(url.endswith('/models') for url, _ in calls))
        self.assertIn('native-517', {row['id'] for row in rows})

    def test_failed_refresh_preserves_snapshot_and_marks_it(self):
        def fail(*args):
            raise RuntimeError('simulated-secret-must-not-leak')
        result = self.store.query(self.entry, CatalogRequest(connection_id='unit-connection', role='vision', refresh=True), fail)
        self.assertEqual(result['source'], 'workspace_snapshot')
        self.assertNotIn('simulated-secret', str(result))
        self.assertTrue(any('失败' in warning for warning in result['warnings']))

    def test_incomplete_directory_is_not_published(self):
        def get(url, params):
            if '/compatible-mode/' in url:
                return {'data': [], 'has_more': False}
            return {'data': {'models': [], 'total': 518}}
        with self.assertRaises(ValueError):
            self.store.refresh(self.entry, get)
        self.assertFalse(self.store.entries)

    def test_execution_rejects_known_wrong_role_and_retired(self):
        config = legacy_config({'text_model': 'unit', 'vision_model': 'unit', 'generate_talking': False}, 'unit-connection')
        record = {'id': 'unit', 'capabilities': ['TG'], 'sources': ['compatible', 'native'], 'offline_time': None}
        self.store.entries['unit-connection'] = {'items': [record], 'connection_identity': tuple(self.entry.get(field) for field in ('provider', 'scope_id', 'compatible_base_url', 'native_http_base_url'))}
        self.store.validate(config, ['text'], {'unit-connection': self.entry})
        with self.assertRaises(ConfigurationError):
            self.store.validate(config, ['vision'], {'unit-connection': self.entry})
        record['offline_time'] = '2000-01-01 00:00:00'
        with self.assertRaises(ConfigurationError):
            self.store.validate(config, ['text'], {'unit-connection': self.entry})

    def test_cache_and_annotations_do_not_cross_connection_scope(self):
        compatible = {'id': 'compatible', 'provider': 'openai_compatible', 'scope_id': 'scope-first', 'compatible_base_url': 'http://localhost:9000/v1'}
        self.store.refresh(compatible, lambda *args: {'data': [{'id': 'unit'}], 'has_more': False})
        self.assertIsNotNone(self.store.current(compatible))
        self.assertIsNone(self.store.current({**compatible, 'scope_id': 'scope-other'}))
        snapshots = {'compatible': dict(compatible)}
        self.store.annotate(snapshots)
        self.assertEqual(snapshots['compatible']['catalog_source'], 'live_directory')
        self.assertNotIn('api_key', str(snapshots))
        self.store.invalidate('compatible')
        self.assertIsNone(self.store.current(compatible))

    def test_all_catalog_keeps_prices_and_role_specific_adapter_guards(self):
        def get(url, params):
            if '/compatible-mode/' in url:
                return {'data': [{'id': 'qwen3-vl-plus'}, {'id': 'other-vl'}]}
            return {'output': {'total': 3, 'models': [
                {'model': 'qwen3-vl-plus', 'capabilities': ['TG', 'VU'], 'prices': [
                    {'range_name': '32k<Input<=128k', 'prices': [
                        {'type': 'input_token', 'price': '0.8', 'price_unit': '每百万tokens', 'price_name': '输入', 'time_band': 'standard'},
                        {'type': 'output_token', 'price': '0', 'price_unit': '每百万tokens', 'price_name': '输出'},
                        {'type': 'bad', 'price': 'NaN'}, {'type': 'negative', 'price': '-1'}]}]},
                {'model': 'other-vl', 'capabilities': ['VU']},
                {'model': 'image-only', 'capabilities': ['IG']}]}}
        result = self.store.query(self.entry, CatalogRequest(connection_id=self.entry['id'], role='all', include_retiring=True), get)
        self.assertEqual(result['source'], 'live_directory')
        self.assertEqual(result['total'], 3)
        rows = {row['id']: row for row in result['items']}
        self.assertTrue(rows['qwen3-vl-plus']['role_support']['grounding']['selectable'])
        self.assertFalse(rows['other-vl']['role_support']['grounding']['selectable'])
        self.assertTrue(rows['other-vl']['role_support']['vision']['selectable'])
        self.assertEqual(rows['image-only']['roles'], [])
        pricing = rows['qwen3-vl-plus']['pricing']
        self.assertEqual(pricing['currency'], 'CNY')
        self.assertEqual(pricing['groups'][0]['range_name'], '32k<Input<=128k')
        self.assertEqual([row['price'] for row in pricing['groups'][0]['prices']], ['0.8', '0'])
        self.assertEqual(pricing['groups'][0]['prices'][0]['price_unit'], '每百万tokens')
        self.assertIsNotNone(pricing['queried_at'])
        cached = self.store.query(self.entry, CatalogRequest(connection_id=self.entry['id'], role='all'), lambda *args: self.fail('fresh cache must not query HTTP'))
        self.assertEqual(cached['total'], 3)


if __name__ == '__main__':
    unittest.main()
