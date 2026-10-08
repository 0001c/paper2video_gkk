"""源文修正入口与创建前门禁：全部隔离，不调用模型。"""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'scripts'), str(ROOT / 'Paper2Video-main/src')]
import paper_ingest
from web import app as web
from fastapi import HTTPException
from fastapi.testclient import TestClient


class SourceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.work = Path(self.temp.name, 'fixture'); self.work.mkdir()
        self.ingest = self.work / 'ingest'; self.ingest.mkdir()
        (self.ingest / 'paper_text.md').write_text('[源文第 5 页]\n公式【未识别字形】', encoding='utf-8')
        (self.ingest / 'ingest_meta.json').write_text(json.dumps({'review_pages': [5]}), encoding='utf-8')
        self.client = TestClient(web.app, client=('127.0.0.1', 51000)); self.addCleanup(self.client.close)

    def test_source_and_page_are_bounded_read_only(self):
        with patch.object(web, '_upload_work', side_effect=lambda value: str(self.work) if value == 'fixture' else None):
            result = self.client.get('/api/ingest/fixture/source')
            self.assertEqual(result.status_code, 200)
            self.assertEqual(result.json()['review_pages'], [5])
            self.assertIn('【未识别字形】', result.json()['text'])
            self.assertEqual(self.client.get('/api/ingest/unknown/source').status_code, 404)
            self.assertEqual(self.client.get('/api/ingest/fixture/pages/-1').status_code, 404)
            self.assertEqual(self.client.get('/api/ingest/fixture/pages/10001').status_code, 404)

    def test_unreadable_full_source_creates_no_job_or_credentials(self):
        with patch.object(web, '_upload_work', return_value=str(self.work)), patch.object(web, '_configure_job_models', side_effect=AssertionError('must reject before model config')):
            response = self.client.post('/api/jobs', json={'ingest_id': 'fixture', 'config': {}})
        self.assertEqual(response.status_code, 422)
        self.assertIn('source：', response.json()['detail'])
        self.assertIn('第 5 页', response.json()['detail'])
        self.assertIn('尚未创建任务', response.json()['detail'])
        self.assertEqual(list(self.work.iterdir()), [self.ingest])

    def test_selected_clean_section_can_pass_source_gate(self):
        with patch.object(web, '_upload_work', return_value=str(self.work)), patch.object(web.course, 'build_plan', return_value=({'token': 'confirmed'}, '已核对的干净正文')):
            with patch.object(web, '_configure_job_models', side_effect=HTTPException(422, detail='model-config-reached')) as configure:
                response = self.client.post('/api/jobs', json={'ingest_id': 'fixture', 'course_plan_token': 'confirmed', 'config': {'course': {}}})
        self.assertEqual(response.json()['detail'], 'model-config-reached')
        configure.assert_called_once()

    def test_utf8_corrected_text_preserves_symbols_and_rejects_binary(self):
        filename = self.work / 'corrected.txt'
        original = '[源文第 5 页]\n公式 y=max(0,x)，误差15.3%。'
        filename.write_text(original, encoding='utf-8')
        text, meta = paper_ingest.ingest(str(filename), str(self.work / 'new-ingest'))
        self.assertEqual(text, original)
        self.assertEqual(meta['format'], 'text')
        filename.write_bytes(b'\xff\x00binary')
        self.assertEqual(paper_ingest.sniff_format(filename), 'unknown')
        filename.write_text('正文\x02公式', encoding='utf-8')
        self.assertEqual(paper_ingest.sniff_format(filename), 'unknown')


if __name__ == '__main__':
    unittest.main(verbosity=2)
