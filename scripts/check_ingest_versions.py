"""提取版本不可变缓存回归：临时目录、隔离 TestClient，不调用模型或正式服务。"""
import asyncio
import io
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'scripts'), str(ROOT / 'Paper2Video-main/src')]
import paper_ingest
from web import app as web
from fastapi import HTTPException, UploadFile
from fastapi.testclient import TestClient


def snapshot(directory):
    return {str(path.relative_to(directory)): path.read_bytes()
            for path in directory.rglob('*') if path.is_file()}


class IngestVersions(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='ingest-versions-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.calls = 0
        self.content = b'%PDF-isolated-fixture'
        self.real_ingest = paper_ingest.ingest
        patches = [patch.multiple(web, JOBS_ROOT=str(self.root),
                                  CONTENT_ROOT=str(self.root / '_content'), _INGEST_LOCK=asyncio.Lock()),
                   patch.object(paper_ingest, 'EXTRACTOR_VERSION', 'fixture-v1'),
                   patch.object(paper_ingest, 'ingest', side_effect=self.fake_ingest)]
        for pending in patches:
            pending.start()
            self.addCleanup(pending.stop)
        self.client = TestClient(web.app, client=('127.0.0.1', 51000))
        self.addCleanup(self.client.close)

    def fake_ingest(self, source, output):
        self.calls += 1
        output = Path(output)
        output.mkdir(parents=True)
        text = '# 第一章\n本节正文，提取版本：' + paper_ingest.EXTRACTOR_VERSION + '。\n'
        text_file = output / 'paper_text.md'
        text_file.write_text(text, encoding='utf-8')
        image = output / 'source_pages/page-0001.png'
        image.parent.mkdir()
        image.write_bytes(b'original-page-image')
        meta = {'source': str(Path(source).absolute()), 'text_path': str(text_file),
                'detected_format': 'pdf', 'needs_llm': True, 'chars': len(text),
                'page_quality': [{'page': 1, 'source_image': str(image)}],
                'glyph_images': [{'path': 'glyph_images/formula.png'}], 'review_pages': []}
        (output / 'ingest_meta.json').write_text(json.dumps(meta), encoding='utf-8')
        # 提取尚未完成时，不能有可被另一读取方使用的最终素材目录。
        if self.calls == 1:
            self.assertEqual(list(self.root.glob('ingest_extract_*')), [])
        return text, meta

    def upload(self, filename='原章节.pdf'):
        return self.client.post('/api/ingest', files={'paper': (filename, self.content, 'application/pdf')})

    def work(self, response):
        return Path(web._upload_work(response['ingest_id']))

    def test_first_upload_and_repeat_reuse_immutable_version_and_raw_file(self):
        first = self.upload().json()
        old = snapshot(self.work(first))
        second = self.upload('改名章节.pdf').json()
        self.assertFalse(first['cached'])
        self.assertTrue(second['cached'])
        self.assertEqual(first['ingest_id'], second['ingest_id'])
        self.assertTrue(first['ingest_id'].startswith('ingest_extract_'))
        self.assertNotEqual(first['ingest_id'], first['source_content_id'])
        self.assertEqual(snapshot(self.work(first)), old)
        self.assertEqual(self.calls, 1)
        self.assertEqual(list((self.root / '_content').glob('*/input/*')), [Path(first['source_path'])])
        self.assertEqual(Path(first['source_path']).read_bytes(), self.content)
        raw_meta = web._content_meta(web._upload_work(first['source_content_id']))
        self.assertIn('改名章节.pdf', raw_meta['original_names'])

    def test_upgrade_preserves_legacy_reviewed_material_and_confirmed_plan(self):
        first = self.upload().json()
        original_work = self.work(first)
        raw_work = Path(web._upload_work(first['source_content_id']))
        (raw_work / 'ingest').mkdir()
        (raw_work / 'ingest/paper_text.md').write_text('旧版人工修改正文', encoding='utf-8')
        (raw_work / 'ingest/ingest_meta.json').write_text('{"legacy":true}', encoding='utf-8')
        raw_meta = web._content_meta(raw_work)
        raw_meta.update(extractor_version='legacy', reviewed_source=True, review_pages=[99])
        web._write_content_meta(raw_work, raw_meta)
        review = self.client.post('/api/ingest/' + first['ingest_id'] + '/review',
                                  json={'text': '手工删除页眉后的已核对正文。'}).json()
        self.assertTrue(review['ok'])
        settings = {'section_ids': ['all']}
        plan = web.course.build_plan(web._course_source(original_work), settings, 4, 'zh')[0]
        historical_job = self.root / 'job_historical'
        historical_job.mkdir()
        shutil.copytree(original_work / 'ingest', historical_job / 'ingest')
        (historical_job / 'course_plan.json').write_text(json.dumps(plan), encoding='utf-8')
        protected = [original_work, self.work(review), raw_work / 'ingest', historical_job]
        before = [snapshot(folder) for folder in protected]
        with patch.object(paper_ingest, 'EXTRACTOR_VERSION', 'fixture-v2'):
            second = self.upload().json()
        self.assertNotEqual(first['ingest_id'], second['ingest_id'])
        self.assertFalse(second['cached'])
        self.assertEqual([snapshot(folder) for folder in protected], before)
        self.assertNotIn('reviewed_source', web._content_meta(self.work(second)))
        self.assertEqual(web._content_meta(self.work(second))['review_pages'], [])
        new_plan = web.course.build_plan(web._course_source(self.work(second)), settings, 4, 'zh')[0]
        self.assertNotEqual(plan['token'], new_plan['token'])
        with patch.object(web, '_configure_job_models', side_effect=AssertionError('must reject before model setup')):
            rejected = self.client.post('/api/jobs', json={
                'ingest_id': second['ingest_id'], 'course_plan_token': plan['token'],
                'config': {'course': settings, 'slide_count': 4, 'script_lang': 'zh'}})
        self.assertEqual(rejected.status_code, 409, rejected.text)
        self.assertEqual(snapshot(historical_job), before[-1])

    def test_failed_upgrade_cleans_only_staging_and_can_retry(self):
        first = self.upload().json()
        before = snapshot(self.work(first))
        def fail(source, output):
            self.fake_ingest(source, output)
            raise RuntimeError('isolated extraction failure')
        with patch.object(paper_ingest, 'EXTRACTOR_VERSION', 'fixture-v2'):
            with patch.object(paper_ingest, 'ingest', side_effect=fail):
                failed = self.upload()
            self.assertEqual(failed.status_code, 400)
            self.assertEqual(snapshot(self.work(first)), before)
            self.assertEqual(Path(first['source_path']).read_bytes(), self.content)
            self.assertEqual(len(list(self.root.glob('ingest_extract_*'))), 1)
            self.assertEqual(list(self.root.glob('.ingest-extract-*')), [])
            retry = self.upload().json()
            self.assertFalse(retry['cached'])
            self.assertTrue(self.upload().json()['cached'])
        self.assertEqual(snapshot(self.work(first)), before)

    def test_first_failure_keeps_original_and_publishes_no_partial_result(self):
        with patch.object(paper_ingest, 'ingest', side_effect=RuntimeError('isolated failure')):
            result = self.upload()
        self.assertEqual(result.status_code, 400)
        self.assertEqual(list(self.root.glob('ingest_extract_*')), [])
        self.assertEqual(list(self.root.glob('.ingest-extract-*')), [])
        originals = list((self.root / '_content').glob('*/input/*'))
        self.assertEqual(len(originals), 1)
        self.assertEqual(originals[0].read_bytes(), self.content)
        self.assertTrue(self.upload().json()['ok'])

    def test_published_metadata_paths_and_source_review_remain_valid(self):
        first = self.upload().json()
        work = self.work(first)
        meta = json.loads((work / 'ingest/ingest_meta.json').read_text(encoding='utf-8'))
        self.assertEqual(meta['source'], first['source_path'])
        self.assertEqual(Path(meta['text_path']), work / 'ingest/paper_text.md')
        self.assertTrue(Path(meta['page_quality'][0]['source_image']).is_file())
        self.assertNotIn('.ingest-extract-', json.dumps(web._content_meta(work)))
        source = self.client.get('/api/ingest/' + first['ingest_id'] + '/source')
        self.assertEqual(source.status_code, 200)
        image = self.client.get(source.json()['pages'][0]['url'])
        self.assertEqual(image.content, b'original-page-image')

    def test_latex_empty_preview_cache_keeps_valid_main_and_course_source(self):
        self.content = b'\\documentclass{article}\n\\begin{document}Chapter source\\end{document}'
        with patch.object(paper_ingest, 'ingest', wraps=self.real_ingest) as ingest:
            first = self.upload('chapter.tex').json()
            second = self.upload('chapter.tex').json()
        self.assertFalse(first['needs_llm'])
        self.assertTrue(second['cached'])
        self.assertEqual(ingest.call_count, 1)
        self.assertTrue(Path(second['latex_root'], 'main.tex').is_file())
        self.assertNotIn('.ingest-extract-', second['latex_root'])
        self.assertIn('Chapter source', web._course_source(self.work(second)))

    def test_invalid_existing_snapshot_is_not_overwritten(self):
        first = self.upload().json()
        work = self.work(first)
        (work / 'ingest/paper_text.md').unlink()
        before = snapshot(work)
        result = self.upload()
        self.assertEqual(result.status_code, 400)
        self.assertEqual(snapshot(work), before)
        self.assertEqual(self.calls, 1)

    def test_same_version_concurrent_requests_share_one_complete_snapshot(self):
        async def upload_pair():
            files = [UploadFile(filename='parallel.pdf', file=io.BytesIO(self.content)) for _ in range(2)]
            try:
                return await asyncio.gather(*(web.api_ingest(file) for file in files))
            finally:
                for file in files:
                    await file.close()
        first, second = asyncio.run(upload_pair())
        self.assertEqual(first['ingest_id'], second['ingest_id'])
        self.assertEqual(sorted((first['cached'], second['cached'])), [False, True])
        self.assertEqual(self.calls, 1)
        self.assertEqual(list(self.root.glob('.ingest-extract-*')), [])

    def layout_ingest(self, source, output):
        _, meta = self.fake_ingest(source, output)
        text = ('## 2.1 构建过程\n正文知识。\n2023.09 图中日期。\n'
                '3.5 模型断行正文。\n## 2.2 扩展法则\n扩展规律。')
        Path(output, 'paper_text.md').write_text(text, encoding='utf-8')
        meta.update(format='pdf', chars=len(text), layout_headings=[
            {'page': 1, 'text': '2.1 构建过程', 'bbox': [10, 50, 150, 65], 'level': 2},
            {'page': 1, 'text': '2.2 扩展法则', 'bbox': [10, 150, 150, 165], 'level': 2}])
        return text, meta

    def assert_course_api_consistency(self, material, expected_layout):
        work = self.work(material)
        text = web._course_source(work)
        self.assertEqual(web._course_layout_options(work), {'layout_headings': expected_layout})
        listing = self.client.get('/api/course/source/' + material['ingest_id'])
        self.assertEqual(listing.status_code, 200, listing.text)
        sections = listing.json()['sections']
        self.assertEqual(sections, web.course.source_sections(text, layout_headings=expected_layout))
        titles = [section['title'] for section in sections]
        self.assertEqual(any(title.startswith('2023.09') for title in titles), not expected_layout)
        self.assertEqual(any(title.startswith('3.5 ') for title in titles), not expected_layout)
        settings = {'section_ids': [sections[1]['id']]}
        expected, _ = web.course.build_plan(text, settings, 4, 'zh', layout_headings=expected_layout)
        preview = self.client.post('/api/course/plan', json={
            'ingest_id': material['ingest_id'], 'settings': settings, 'slide_count': 4, 'script_lang': 'zh'})
        self.assertEqual(preview.status_code, 200, preview.text)
        self.assertEqual(preview.json()['token'], expected['token'])
        payload = {'ingest_id': material['ingest_id'], 'course_plan_token': expected['token'],
                   'config': {'course': settings, 'slide_count': 4, 'script_lang': 'zh'}}
        # 合法 token 必须通过创建端相同校验；在模型配置之前主动停止，零任务写入。
        before = snapshot(self.root)
        with patch.object(web, '_configure_job_models', side_effect=HTTPException(
                422, detail='isolated-model-config-boundary')) as configure:
            accepted = self.client.post('/api/jobs', json=payload)
            self.assertEqual(accepted.status_code, 422, accepted.text)
            self.assertEqual(accepted.json()['detail'], 'isolated-model-config-boundary')
            configure.assert_called_once()
            other, _ = web.course.build_plan(text, settings, 4, 'zh', layout_headings=not expected_layout)
            self.assertNotEqual(expected['token'], other['token'])
            rejected = self.client.post('/api/jobs', json={**payload, 'course_plan_token': other['token']})
            self.assertEqual(rejected.status_code, 409, rejected.text)
            configure.assert_called_once()
        self.assertEqual(snapshot(self.root), before)

    def test_new_pdf_layout_source_plan_and_job_tokens_use_same_explicit_headings(self):
        with patch.object(paper_ingest, 'ingest', side_effect=self.layout_ingest):
            material = self.upload().json()
        self.assert_course_api_consistency(material, True)

    def test_legacy_and_reviewed_sources_retain_default_heading_rules(self):
        with patch.object(paper_ingest, 'ingest', side_effect=self.layout_ingest):
            material = self.upload().json()
        original = self.work(material)
        text = web._course_source(original)
        meta = json.loads((original / 'ingest/ingest_meta.json').read_text(encoding='utf-8'))
        for name, metadata in [('legacy', {'format': 'pdf'}),
                               ('reviewed', {**meta, 'reviewed_source': True})]:
            with self.subTest(name=name):
                work = self.root / name / 'ingest'
                work.mkdir(parents=True)
                (work / 'paper_text.md').write_text(text, encoding='utf-8')
                (work / 'ingest_meta.json').write_text(json.dumps(metadata), encoding='utf-8')
                self.assert_course_api_consistency({'ingest_id': name}, False)
        before = snapshot(original)
        reviewed = self.client.post('/api/ingest/' + material['ingest_id'] + '/review',
                                    json={'text': text + '\n人工核对后的正文。'})
        self.assertEqual(reviewed.status_code, 200, reviewed.text)
        self.assertTrue(web._content_meta(self.work(reviewed.json()))['reviewed_source'])
        self.assert_course_api_consistency(reviewed.json(), False)
        self.assertEqual(snapshot(original), before)


if __name__ == '__main__':
    unittest.main(verbosity=2)
