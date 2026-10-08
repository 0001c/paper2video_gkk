"""P1/P2 离线验收，禁止真实模型请求；实际 PDF、字词及小视频回归。"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT),str(ROOT/'scripts'),str(ROOT/'Paper2Video-main/src')]
import pv_env
from PIL import Image
import bailian_speech as speech
import cursor_gen
import cursor_render
import caption_timing
import subtitle_render
import paper_figures
import slide_quality
import teaching_quality
import merge_video
import check_grounding_segments
from spoken_text import prepare_spoken
OLD = ROOT/'jobs/_validation/nlp_chapter2_2026-10-07'
SOURCE = Path('F:/Documents/提取自自然语言处理第二章.pdf')


class Checks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='p1p2-',dir=ROOT/'jobs/_validation')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        environment = patch.dict(os.environ, {'PAPER2VIDEO_ROLE_GROUNDING_API_KEY':'fake-ground-key','PAPER2VIDEO_USAGE_DIR':str(self.root/'costs')})
        environment.start()
        self.addCleanup(environment.stop)
        temporary_path = patch.object(pv_env,'TMP_DIR',str(self.root))
        temporary_path.start()
        self.addCleanup(temporary_path.stop)

    @unittest.skipUnless(SOURCE.is_file(),'本机章节未提供')
    def test_full_chapter_figures_and_budget_cache(self):
        figures,_ = paper_figures.extract(str(SOURCE),str(self.root),log=lambda _:None,max_figures=12)
        self.assertEqual(len(figures),12)
        self.assertIn(16,[fig['page'] for fig in figures])
        figures,_ = paper_figures.extract(str(SOURCE),str(self.root),log=lambda _:None)
        self.assertEqual(len(figures),22)
        self.assertEqual({fig['figure_id'] for fig in figures},{'F2-'+str(i) for i in range(1,23)})
        self.assertEqual([f['figure_id'] for f in figures if f['kind']=='composite'],['F2-16','F2-17','F2-18'])
        block = paper_figures.figure_prompt_block(self.root)
        self.assertIn('source_page=15',block)
        self.assertIn('F2-13',block)

    def test_quality_rejects_overflow_wrong_id_and_semantic_caption(self):
        figure = {'rel':'figures/fig15_p14.png','figure_id':'F2-15','caption':'图2-15 神经网络结构图','page':14}
        frame = '\\begin{frame}{网络}\\begin{figure}\n% source-figure: F2-15\n\\includegraphics{figures/fig15_p14.png}\\caption{图2-15 神经网络结构图}\\end{figure}\\end{frame}'
        self.assertEqual(slide_quality.quality_errors(frame,figures=[figure]),[])
        self.assertTrue(slide_quality.quality_errors(frame.replace('{网络}','{山谷示意图}'),figures=[figure]))
        self.assertTrue(slide_quality.quality_errors(frame,'warning: Overfull \\vbox (20pt too high)'))
        self.assertTrue(slide_quality.quality_errors(frame.replace('图2-15 神经网络结构图','图2-15 山谷示意图'),figures=[figure]))
        self.assertTrue(slide_quality.quality_errors(frame.replace('F2-15','F2-13').replace('图2-15','图2-13'),figures=[figure]))
        self.assertTrue(slide_quality.quality_errors(frame,figures=[]))

    def test_compile_success_cannot_bypass_quality_or_keep_generating(self):
        import slide_code_gen_select_improvement as slides
        target = self.root/'slides.tex'
        code = '\\documentclass{beamer}\\begin{document}\\begin{frame}bad\\end{frame}\\end{document}'
        target.write_text(code,encoding='utf-8')
        process = SimpleNamespace(returncode=0,stdout='',stderr='warning: Overfull \\vbox (40pt too high)')
        with patch.object(slides.subprocess,'run',return_value=process):
            ok, feedback = slides.compile_tex(target)
        self.assertFalse(ok)
        self.assertIn('error:',feedback)
        with self.assertRaisesRegex(ValueError,'收费前停止'):
            slide_quality.require_quality(self.root)
        prompt = self.root/'prompt.txt';prompt.write_text('生成课件',encoding='utf-8')
        agent = MagicMock()
        agent.step.return_value = SimpleNamespace(msgs=[SimpleNamespace(content=code)],info={'usage':{}})
        with patch.object(slides,'create_agent',return_value=agent),patch.object(slides,'compile_tex',return_value=(False,feedback)):
            with self.assertRaisesRegex(RuntimeError,'质量门禁'):
                slides.latex_code_gen_upgrade(prompt,self.root,target,{}, {},if_fix=False,if_tree_search=False)
        self.assertEqual(agent.step.call_count,1)

    def test_quality_cache_binds_figure_manifest_and_pdf(self):
        import slide_code_gen_select_improvement as slides
        tex = self.root/'slides.tex'
        tex.write_text('\\documentclass{beamer}\\begin{document}\\end{document}',encoding='utf-8')
        tex.with_suffix('.pdf').write_bytes(b'fixture-pdf')
        (self.root/'figures').mkdir()
        index = self.root/'figures/index.json'
        index.write_text('{"figures":[]}',encoding='utf-8')
        slide_quality.record_quality(tex,'',True)
        with patch.object(slides,'compile_tex') as compiler:
            slide_quality.require_quality(self.root)
            compiler.assert_not_called()
        for target in (index,tex.with_suffix('.pdf')):
            original = target.read_bytes()
            target.write_bytes(original+b' ')
            with patch.object(slides,'compile_tex',return_value=(False,'changed dependency')) as compiler:
                with self.assertRaisesRegex(ValueError,'收费前停止'):
                    slide_quality.require_quality(self.root)
                compiler.assert_called_once_with(tex)
            target.write_bytes(original)

    def test_no_skips_pdf_grounding_and_stale_point_cache(self):
        images,audio = self.root/'images',self.root/'audio';images.mkdir();audio.mkdir()
        Image.new('RGB',(200,100),'white').save(images/'1.png')
        script = self.root/'script.txt';script.write_text('讲解。 | no\n目标。 | "目标"',encoding='utf-8')
        speech.write_pair(audio/'0.wav',bytes(48000*2),{'segments':[{'words':[
            {'word':'讲解','start':0.1,'end':0.3},{'word':'目标','start':1.1,'end':1.4}]}]},'讲解。\n\n\n目标。')
        locator = MagicMock();locator.locate.return_value = [50,40]
        import bailian_grounding
        output = self.root/'cursor.json'
        with patch.object(cursor_gen,'_open_pdf_locator',return_value=locator),patch.object(bailian_grounding,'infer',side_effect=AssertionError('不得请求')):
            for _ in range(2):
                cursor_gen.cursor_gen_per_sentence(script,images,audio,str(output),resume_cache=True,timing_source='tts_timestamp',model_snapshot=check_grounding_segments.snapshot())
        self.assertEqual(locator.locate.call_count,1)
        timeline = json.loads(output.read_text())
        self.assertIsNone(timeline[0]['cursor'])
        self.assertEqual(timeline[1]['cursor'],[50,40])
        self.assertTrue(timeline[1]['word_spans'])
        self.assertEqual(json.loads(output.with_name('cursor_mid.json').read_text())[0]['source'],'hidden')

    def test_long_captions_use_actual_word_times_and_keep_percent(self):
        original = '错误率15.3%，计算η=0.1。'+'逐步解释神经网络的计算方法。'*12
        oral,_ = prepare_spoken(original)
        chars = speech.plain_text(oral)
        words = [{'word':char,'start':i*0.19,'end':i*0.19+0.12} for i,char in enumerate(chars)]
        timing = {'spoken_text':oral,'segments':[{'words':words}]}
        spans = caption_timing.sentence_word_spans([original],timing)[0]
        segment = {'text':original,'start':0,'end':words[-1]['end']+0.3,'word_spans':spans}
        captions = subtitle_render.timed_captions(segment,700,32)
        self.assertEqual(''.join(c['text'] for c in captions),original)
        self.assertGreater(len(captions),2)
        self.assertTrue(any('15.3%' in c['text'] for c in captions))
        self.assertTrue(all(len(subtitle_render._wrap_text(c['text'],700,32).splitlines())<=2 for c in captions))
        ends = {w['end'] for w in words}|{segment['end']}
        self.assertTrue(all(c['end'] in ends for c in captions))
        self.assertTrue(all(a['end']<=b['start'] for a,b in zip(captions,captions[1:])))

    def test_no_timestamps_no_average_split(self):
        with self.assertRaisesRegex(ValueError,'禁止按字数平均'):
            subtitle_render.timed_captions({'text':'长字幕'*90,'start':0,'end':90},600,32)

    def test_hidden_cursor_is_absent_in_decoded_video(self):
        source,target = self.root/'source.mp4',self.root/'cursor.mp4'
        image = self.root/'cursor.png'
        Image.new('RGBA',(16,16),'red').save(image)
        subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','color=white:s=160x90:r=30:d=2','-c:v','libx264','-pix_fmt','yuv420p',source],check=True)
        cursor_render.render_cursor_on_video(str(source),str(target),[(0,80,45),(0.5,None,None),(1.3,80,45),(1.7,None,None)],cursor_size=16,cursor_img_path=str(image))
        raw = subprocess.run(['ffmpeg','-v','error','-i',target,'-f','rawvideo','-pix_fmt','rgb24','-'],check=True,capture_output=True).stdout
        for frame,visible in [(5,True),(25,False),(45,True),(55,False)]:
            pixel = frame*160*90*3+(45*160+80)*3
            red,green,_ = raw[pixel:pixel+3]
            self.assertEqual(red-green>100,visible)
        cursor_render.render_cursor_on_video(str(source),str(source),[(0,None,None)],cursor_img_path=str(image))

    def test_source_examples_correct_result_and_no_fabricated_network_inputs(self):
        text = '[源文第 9 页]中间层计算公式 a=f(xw)。输出层计算公式 Z=f(av)。\n[源文第 15 页]计算 e=(a+b)(b+1)，假设 a=2、b=1。'
        examples = teaching_quality.source_examples(text)
        self.assertEqual(len(examples),2)
        self.assertFalse(examples[0]['numeric_inputs_given'])
        self.assertEqual(examples[1]['source_page'],15)
        self.assertEqual(examples[1]['result']['value'],6)
        self.assertEqual(examples[1]['result']['gradients'],{'a':2,'b':5})
        policy = {'examples':examples,'speech_rate':220,'script_lang':'zh'}
        pages = ['中间层计算隐藏节点，再代入输出层。 | no','e=6，∂e/∂a=2，∂e/∂b=5。 | no']
        good = teaching_quality.script_report(pages,policy,['例题E1：逐层计算','例题E2：链式法则'])
        self.assertTrue(good['passed'],good)
        bad = teaching_quality.script_report([pages[0],pages[1].replace('b=5','b=4')],policy,['例题 E1','例题 E2'])
        self.assertFalse(bad['passed'])
        for wrong in (pages[1].replace('e=6','e=60'),pages[1].replace('b=5','b=50'),pages[1]+' e=7。 | no'):
            self.assertFalse(teaching_quality.script_report([pages[0],wrong],policy,['例题E1','例题E2'])['passed'])
        self.assertFalse(teaching_quality.script_report(pages,policy,[])['passed'])

    def test_arithmetic_never_executes_source_code(self):
        for code in ('__import__("os").system("echo bad")','(1).__class__','2**100000','lambda: 3'):
            with self.assertRaises((ValueError,SyntaxError)):
                teaching_quality._calculate(code,{})

    def test_promises_and_custom_budget_fail_before_paid_work(self):
        plan = {'speech_rate':220,'script_lang':'zh','target_minutes':10,'examples':[]}
        report = teaching_quality.script_report(['稍后实时计算演示。 | no'],plan)
        self.assertFalse(report['passed'])
        self.assertEqual(len(report['issues']),2)

    def test_unsupported_formula_still_opens_review_and_blocks_paid_work(self):
        import course
        page = r'公式 \unknownmath{x}。 | no'
        policy = {'speech_rate':220,'script_lang':'zh','examples':[]}
        report = teaching_quality.script_report([page],policy)
        self.assertFalse(report['passed'])
        self.assertIsNone(report['estimated_minutes'])
        self.assertIn('数学口述',report['issues'][0])
        self.assertGreater(course.estimate_minutes(page,'zh',220),0)

    def test_merge_page_boundaries_and_audio_have_no_accumulated_padding(self):
        images,audio = self.root/'images',self.root/'audio';images.mkdir();audio.mkdir()
        for i,duration in enumerate((1.017,0.923)):
            Image.new('RGB',(320,180),('white','navy')[i]).save(images/f'{i+1}.png')
            speech.write_pair(audio/f'{i}.wav',bytes(round(duration*24000)*2),{'segments':[{'words':[{'word':'测试','start':0.01,'end':0.5}]}]},'测试')
        target = self.root/'output.mp4'
        merge_video.merge_slides_and_audio(str(images),str(audio),str(self.root/'parts'),2,str(target))
        timeline = json.loads((self.root/'parts/media_timeline.json').read_text())
        self.assertLess(timeline['max_boundary_delta_seconds'],1/30)
        probe = json.loads(subprocess.run(['ffprobe','-v','error','-show_entries','stream=codec_type,duration','-of','json',target],check=True,capture_output=True).stdout)
        self.assertLess(abs(float(next(s for s in probe['streams'] if s['codec_type']=='audio')['duration'])-1.94),0.01)
        # 从真实媒体 PTS 与像素确认第二页切换，而非只检查预估帧数。
        raw = subprocess.run(['ffmpeg','-v','error','-i',target,'-f','rawvideo','-pix_fmt','rgb24','-'],check=True,capture_output=True).stdout
        size = 320*180*3
        transition = next(i for i in range(len(raw)//size) if raw[i*size]<100)
        self.assertEqual(transition,timeline['pages'][1]['start_frame'])
        frames = json.loads(subprocess.run(['ffprobe','-v','error','-select_streams','v:0','-show_frames','-show_entries','frame=best_effort_timestamp_time','-of','json',target],check=True,capture_output=True).stdout)['frames']
        boundary = float(frames[transition]['best_effort_timestamp_time'])
        self.assertLess(abs(boundary-timeline['pages'][1]['audio_start_seconds']),1/30)
        self.assertTrue(all(abs(float(frame['best_effort_timestamp_time'])-i/30)<0.000002 for i,frame in enumerate(frames)))


if __name__ == '__main__':
    unittest.main(verbosity=2)
