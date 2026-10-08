"""P0 离线回归：共享讲稿契约、口播映射、源文保真、严格静音补帧。"""

import base64
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'scripts'), str(ROOT / 'Paper2Video-main/src')]
import httpx
import avatar_segments
import bailian_speech as speech
import bailian_grounding
import course
import paper_ingest
import paper_struct
import script_validation as script
from spoken_text import prepare_spoken
from check_bailian_speech import CONNECTION
import check_bailian_speech as speech_checks

EVIDENCE = ROOT / 'jobs/_validation/nlp_chapter2_2026-10-07'
CHAPTER = Path('F:/Documents/提取自自然语言处理第二章.pdf')


class P0Checks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='p0-', dir=ROOT / 'jobs/_validation')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_single_contract_multiline_and_json(self):
        text = '错误率为15.3%。\n| "AlexNet"\n| "2012年"\n接下来继续讲解。\n###\n反向传播更新参数。 | no'
        canonical = script.normalize_script(text)
        self.assertIn('15.3%', canonical)
        self.assertEqual(script.parse_script(canonical)[0][0][0], '错误率为15.3%。')
        self.assertEqual(course.spoken_text(text.split('###')[0]), '错误率为15.3%。\n\n\n接下来继续讲解。')
        structured = json.dumps({'pages':[{'sentences':[{'speech_text':'错误率15.3%。','cursor_prompt':'no'}]}]}, ensure_ascii=False)
        self.assertTrue(script.validate_script_output(structured, 1)[0])
        self.assertEqual(script.normalize_script(structured), '错误率15.3%。 | no')

    def test_truncation_empty_speech_and_extra_page_fail(self):
        for value in ('| no', 'hello | "broken', 'hello | no\nunfinished', 'hello | no\n###\nworld | no'):
            self.assertFalse(script.validate_script_output(value, 1)[0])

    def test_multiline_cursor_and_math_separator(self):
        text = '计算 p(y|x)。\n| "直观理解\n沿坡度方向下落。"'
        parsed = script.parse_script(text)
        self.assertEqual(parsed, [[['计算 p(y|x)。', '"直观理解 沿坡度方向下落。"']]])
        self.assertEqual(script.parse_script(script.normalize_script(text)), parsed)

    @unittest.skipUnless((EVIDENCE/'review-original.json').is_file(), '本机历史讲稿未提供')
    def test_real_original_chapter_script_needs_no_manual_formatting(self):
        review = json.loads((EVIDENCE/'review-original.json').read_text(encoding='utf-8'))
        text = '\n###\n'.join(review['pages'])
        valid,why = script.validate_script_output(text,12)
        self.assertTrue(valid,why)
        normalized = script.normalize_script(text)
        self.assertEqual(len(script.parse_script(normalized)),12)
        self.assertIn('15.3%',normalized)

    def test_review_uses_same_parser(self):
        from web import app as web
        text = '错误率15.3%。\n| "错误率"'
        snapshot = {'editable':True, 'version':'v', 'pages':['old | no']}
        with patch.object(web, '_review_snapshot', return_value=snapshot):
            value = web._check_review({}, SimpleNamespace(version='v',pages=[text]))
        self.assertEqual(value, '错误率15.3%。 | "错误率"')

    def test_course_assembles_structured_pages_and_reuses_cache(self):
        slides = self.root/'slides'
        slides.mkdir()
        for index in range(2):
            (slides/f'{index}.png').write_bytes(b'fake-slide')
        prompt = self.root/'prompt.txt'
        prompt.write_text('讲解当前内容', encoding='utf-8')
        calls = []
        def generate(*args, **kwargs):
            calls.append(kwargs['image_paths'])
            return json.dumps({'pages':[{'sentences':[{'speech_text':'错误率15.3%。','cursor_prompt':'no'}]}]},ensure_ascii=False), {}
        result, _ = course.generate_script(None,slides,prompt,{},self.root,generate)
        self.assertTrue(script.validate_script_output(result,2)[0])
        self.assertNotIn('speech_text',result)
        course.generate_script(None,slides,prompt,{},self.root,generate)
        self.assertEqual(len(calls),2)

    def test_numbers_operators_and_full_original_ranges(self):
        original = '2016年错误率15.3%，η=0.1，δ+σ，编号30。'
        spoken, mapping = prepare_spoken(original)
        self.assertIn('二零一六年', spoken)
        self.assertIn('百分之十五点三', spoken)
        self.assertIn('伊塔等于零点一', spoken)
        self.assertIn('德尔塔加西格玛', spoken)
        self.assertIn('编号三十', spoken)
        for item in mapping:
            self.assertTrue(0 <= item['original_index'] < item['original_end'] <= len(original))
        percent = [m for m in mapping if original[m['original_index']:m['original_end']] == '15.3%']
        self.assertEqual(''.join(m['normalized'] for m in percent), '百分之十五点三')
        self.assertEqual(prepare_spoken('x²+x³+x₀。')[0], 'x平方加x立方加x下标零。')

    def test_original_subtitles_timing_and_cache_reuse(self):
        calls = []
        class FakeSpeech:
            def synthesize(self, model, voice, text, rate, language):
                calls.append(text)
                chars = list(speech.plain_text(text))
                return bytes(24000 * 2), {'segments':[{'words':[
                    {'word':char,'start':index/len(chars)*0.8,'end':(index+1)/len(chars)*0.8}
                    for index,char in enumerate(chars)]}]}
        texts = ['错误率15.3%。', 'η=0.1，δ+σ。']
        parsed = [[(text,'no') for text in texts]]
        snapshot = speech_checks.SpeechTests().snapshot()
        speech.tts_pages(parsed, self.root, snapshot, client=FakeSpeech())
        pcm, timing = speech.read_pair(self.root/'0.wav', '\n\n\n'.join(texts))
        self.assertEqual(timing['text'], '\n\n\n'.join(texts))
        self.assertIn('百分之十五点三', timing['spoken_text'])
        intervals = bailian_grounding.timestamp_sentences(texts, timing, len(pcm)/48000)
        self.assertEqual([x['text'] for x in intervals], texts)
        self.assertLess(intervals[0]['end'], intervals[1]['end'])
        before = len(calls)
        speech.tts_pages(parsed, self.root, snapshot, client=FakeSpeech())
        self.assertEqual(len(calls), before)
        changed = {**timing, 'normalization_map': []}
        with self.assertRaisesRegex(ValueError, '映射无效'):
            speech.validate_timing(changed, pcm, timing['text'])

    def test_whole_chapter_preflight_before_first_paid_request(self):
        class NeverSpeech:
            def synthesize(self, *args):
                raise AssertionError('不得先合成前页')
        with self.assertRaisesRegex(ValueError, '未支持命令'):
            speech.tts_pages([[('前页内容。','no')],[(r'后页 \unknown{x}。','no')]], self.root,
                             speech_checks.SpeechTests().snapshot(), client=NeverSpeech())

    def test_indexed_provisional_ranges_are_replaced(self):
        events = [
            {'output':{'audio':{'data':base64.b64encode(bytes(48000)).decode()},'sentence':{'index':0,'words':[
                {'text':'参数级处理','begin_index':0,'end_index':5,'begin_time':0,'end_time':900}]}}},
            {'output':{'finish_reason':'stop','sentence':{'index':0,'words':[
                {'text':'参数','begin_index':0,'end_index':2,'begin_time':0,'end_time':400},
                {'text':'级处理','begin_index':2,'end_index':5,'begin_time':400,'end_time':900}]},'audio':{'url':'https://secret.invalid/?signature=private'}},'usage':{'characters':5}}]
        body = ''.join('data: '+json.dumps(x)+'\n\n' for x in events)
        with httpx.Client(transport=httpx.MockTransport(lambda r:httpx.Response(200,text=body))) as client:
            pcm,timing = speech.SpeechClient(CONNECTION,'fake',client).synthesize('cosyvoice-v3-flash','longanyang','参数级处理')
        self.assertEqual(''.join(w['word'] for s in timing['segments'] for w in s['words']), '参数级处理')

    def test_diagnostics_exclude_audio_urls_and_keep_usage(self):
        event = {'output':{'finish_reason':'stop','audio':{'data':base64.b64encode(bytes(48000)).decode(),
                'url':'https://secret.invalid/?signature=private'}, 'sentence':{'index':0,'words':[
                {'text':'漏','begin_time':0,'end_time':500}]}},'usage':{'characters':4}}
        with httpx.Client(transport=httpx.MockTransport(lambda r:httpx.Response(200,text='data: '+json.dumps(event)+'\n\n'))) as client:
            with self.assertRaisesRegex(ValueError,'完整映射'):
                speech.SpeechClient(CONNECTION,'fake',client,self.root).synthesize('cosyvoice-v3-flash','longanyang','原文未漏')
        record = json.loads(next((self.root/'model_usage').glob('*.json')).read_text())
        self.assertEqual(record['state'],'succeeded')
        evidence = next((self.root/'tts_diagnostics').glob('*.json')).read_text(encoding='utf-8')
        self.assertNotIn('signature',evidence)
        self.assertNotIn('audio',evidence)
        self.assertIn('provider_word_events',evidence)

    def test_struct_fidelity_rejects_new_fact_and_changed_formula(self):
        source = '正向微分计算各节点对输入的偏导。\n\n公式 e=(a+b)(b+1)，a=2、b=1。'
        candidate = '\\documentclass{article}\n\\begin{document}\n正向微分计算各节点对输出的影响。\n公式e=(a-b)(b+1)。\n\\end{document}'
        info = {'model':'mock','usage':{},'elapsed':0,'finish_reason':'stop'}
        with patch.object(paper_ingest,'ingest',return_value=(source,{'format':'docx','detected_format':'docx'})), patch.object(paper_struct,'call_llm',return_value=(candidate,info)):
            tex,report = paper_struct.structurize('fixture.docx', str(self.root), do_compile=False)
        self.assertTrue(report['fidelity']['literal_fallback'])
        self.assertIn('正向微分计算各节点对输入的偏导',tex)
        self.assertNotIn('各节点对输出',tex)
        self.assertIn('e=(a+b)(b+1)',tex)
        binding = json.loads((self.root/'latex_proj/source_binding.json').read_text(encoding='utf-8'))
        self.assertEqual('\n\n'.join(x['text'] for x in binding['paragraphs']), source)

    def test_reviewed_text_is_local_no_model_rewrite(self):
        source = self.root/'reviewed.md'
        source.write_text('2016年教材原句。\n\n错误率为15.3%。',encoding='utf-8')
        with patch.object(paper_struct,'call_llm',side_effect=AssertionError('不应收费重写审核文本')):
            tex,report = paper_struct.structurize(str(source),str(self.root/'output'),text_file=str(source),do_compile=False)
        self.assertEqual(report['model'],'local-source-layout')
        self.assertIn(r'15.3\%',tex)

    def test_source_binding_rejects_modified_tex_source_and_text(self):
        source = self.root/'reviewed.md'
        original = '原文错误率为15.3%。'
        source.write_text(original, encoding='utf-8')
        paper_struct.structurize(str(source), str(self.root/'output'), text_file=str(source), do_compile=False)
        tex = self.root/'output/latex_proj/main.tex'
        self.assertTrue(paper_struct.verified_source_binding(tex, source, source))
        self.assertFalse(paper_struct.verified_source_binding(tex, self.root/'missing.md'))
        content = tex.read_bytes()
        tex.write_bytes(content+b'\nextra')
        self.assertFalse(paper_struct.verified_source_binding(tex, source, source))
        tex.write_bytes(content)
        source.write_text('原文错误率为13%。', encoding='utf-8')
        self.assertFalse(paper_struct.verified_source_binding(tex, source))
        self.assertFalse(paper_struct.verified_source_binding(tex, text_path=source))
        tex.with_name('source_binding.json').unlink()
        self.assertFalse(paper_struct.verified_source_binding(tex))

    def test_struct_cache_keeps_verifiable_binding(self):
        import artifact_cache
        source = self.root/'source.md'
        source.write_text('原文错误率15.3%。', encoding='utf-8')
        paper_struct.structurize(str(source),str(self.root/'output'),text_file=str(source),do_compile=False)
        tex = self.root/'output/latex_proj/main.tex'
        with patch.object(artifact_cache,'CACHE_ROOT',str(self.root/'cache')):
            facts = {'source_sha1':'fixture-content'}
            cached = artifact_cache.publish_struct(facts,str(tex))
            hit, _, _ = artifact_cache.lookup_struct(facts)
        self.assertEqual(Path(hit),Path(cached)/'main.tex')
        self.assertTrue(paper_struct.verified_source_binding(hit,source,source))

    def test_failed_compile_cannot_be_reused_as_valid_struct(self):
        source = self.root/'source.md'
        source.write_text('教材原句。',encoding='utf-8')
        with patch.object(paper_struct,'compile_tex',return_value=(False,'fixture compile failure')):
            with self.assertRaisesRegex(RuntimeError,'编译失败'):
                paper_struct.structurize(str(source),str(self.root/'output'),text_file=str(source))
        self.assertFalse(paper_struct.verified_source_binding(self.root/'output/latex_proj/main.tex',source))

    @unittest.skipUnless(CHAPTER.is_file(), '本机章节 PDF 未提供')
    def test_chapter_reordering_and_quality_gate_no_paid_request(self):
        text,meta = paper_ingest.extract_pdf(str(CHAPTER))
        repaired = [x['page'] for x in meta['page_quality'] if x['reading_order_repaired']]
        self.assertEqual(repaired,[8,9,10,14,15])
        self.assertTrue({5,6,7,8,9,14,15}.issubset(set(meta['review_pages'])))
        self.assertIn('两层神经网络',text)
        self.assertIn('[源文第 15 页]',text)
        with patch.object(paper_struct,'call_llm',side_effect=AssertionError('坏公式必须先阻断')):
            with self.assertRaisesRegex(RuntimeError,'收费结构化前停止'):
                paper_struct.structurize(str(CHAPTER),str(self.root),do_compile=False)
        self.assertTrue((self.root/'ingest/source_pages/page-0015.png').is_file())

    def test_wan_silent_tail_passes_real_saved_media(self):
        wan = EVIDENCE/'wan_short'
        if not (wan/'avatar.mp4').is_file():
            self.skipTest('本机 Wan 归档素材未提供')
        pcm,timing = speech.read_pair(wan/'speech.wav')
        manifest = avatar_segments.prepare(wan/'speech.wav', self.root/'parts', timing=timing)
        output = avatar_segments.stitch([wan/'avatar.mp4'],manifest,wan/'speech.wav',self.root/'stitched.mp4')
        data = json.loads(subprocess.run(['ffprobe','-v','error','-show_entries','format=duration:stream=codec_type','-of','json',output],capture_output=True,check=True).stdout)
        self.assertLess(abs(float(data['format']['duration'])-6.32),0.08)
        self.assertEqual(sum(s['codec_type']=='audio' for s in data['streams']),1)

    def test_shortened_voice_or_loud_tail_still_rejected(self):
        for last_word, loud in [(6.20,False),(5.92,True)]:
            count = int(6.32*24000)
            pcm = bytearray(count*2)
            if loud:
                pcm[int(6.13*24000)*2:] = b'\x00\x10' * (count-int(6.13*24000))
            audio = self.root/'master.wav'
            timing = {'segments':[{'words':[{'word':'讲话','start':0,'end':last_word}]}]}
            speech.write_pair(audio,bytes(pcm),timing,'讲话')
            manifest = avatar_segments.prepare(audio,self.root/'parts',timing=speech.read_pair(audio)[1])
            probe = SimpleNamespace(stdout=json.dumps({'streams':[{'duration':'6.125'}]}).encode())
            with patch.object(avatar_segments.subprocess,'run',return_value=probe):
                with self.assertRaisesRegex(ValueError,'禁止补帧'):
                    avatar_segments.stitch(['mock.mp4'],manifest,audio,self.root/'bad.mp4')


if __name__ == '__main__':
    unittest.main(verbosity=2)
