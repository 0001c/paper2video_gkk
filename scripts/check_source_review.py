"""源文修正入口与创建前门禁：全部隔离，不调用模型。"""
import json
import asyncio
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, AsyncMock

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

    def test_review_preserves_source_images_and_is_immutable_and_idempotent(self):
        source = self.work / 'original.pdf'
        source.write_bytes(b'%PDF-original-fixture')
        pages = self.ingest / 'source_pages'; pages.mkdir()
        (pages / 'page-0005.png').write_bytes(b'original-page-image')
        (self.ingest / 'ingest_meta.json').write_text(json.dumps({'source': str(source), 'review_pages': [5]}), encoding='utf-8')
        original = (self.ingest / 'paper_text.md').read_text(encoding='utf-8')
        with patch.object(web, 'JOBS_ROOT', self.temp.name), patch.object(web, '_upload_work', return_value=str(self.work)):
            first = self.client.post('/api/ingest/fixture/review', json={'text': '已核对公式 y=max(0,x)'})
            self.assertEqual(first.status_code, 200, first.text)
            second = self.client.post('/api/ingest/fixture/review', json={'text': '已核对公式 y=max(0,x)'})
        self.assertFalse(first.json()['cached'])
        self.assertTrue(second.json()['cached'])
        self.assertEqual(first.json()['ingest_id'], second.json()['ingest_id'])
        target = Path(self.temp.name, first.json()['ingest_id'])
        self.assertEqual(Path(first.json()['source_path']).read_bytes(), source.read_bytes())
        self.assertEqual((target / 'ingest/source_pages/page-0005.png').read_bytes(), b'original-page-image')
        meta = json.loads((target / 'ingest/ingest_meta.json').read_text(encoding='utf-8'))
        self.assertTrue(meta['reviewed_source'])
        self.assertEqual(meta['review_pages'], [])
        self.assertEqual((self.ingest / 'paper_text.md').read_text(encoding='utf-8'), original)

    def test_review_rejects_unresolved_text_and_missing_source_without_writes(self):
        with patch.object(web, 'JOBS_ROOT', self.temp.name), patch.object(web, '_upload_work', return_value=str(self.work)):
            for text, field in [('公式【未识别字形】', 'text'), ('已核对正文', 'source')]:
                result = self.client.post('/api/ingest/fixture/review', json={'text': text})
                self.assertEqual(result.status_code, 422)
                self.assertTrue(result.json()['detail'].startswith(field + '：'))
        self.assertEqual(list(Path(self.temp.name).iterdir()), [self.work])

    def test_reviewed_non_course_job_uses_corrected_text_in_structuring(self):
        source = self.work / 'original.pdf'; source.write_bytes(b'%PDF-fixture')
        (self.ingest / 'ingest_meta.json').write_text('{"reviewed_source": true}', encoding='utf-8')
        job = {'id': 'fixture', 'source_path': str(source), 'config': {}}
        process = AsyncMock(); process.wait.return_value = 1
        with patch.object(web, '_set_phase', new_callable=AsyncMock), patch.object(web.asyncio, 'create_subprocess_exec', new_callable=AsyncMock, return_value=process) as spawn:
            self.assertFalse(asyncio.run(web._prepare_latex(job, str(self.work), io.StringIO(), {})))
        args = spawn.call_args.args
        self.assertEqual(args[args.index('--text-file') + 1], str(self.ingest / 'paper_text.md'))


if __name__ == '__main__':
    unittest.main(verbosity=2)
