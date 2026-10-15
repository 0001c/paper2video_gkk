"""教学上下文、确定性计算、修正恢复和真实音频时长回归；不调用模型。"""
import sys
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'Paper2Video-main/src'))
import course
import teaching_quality
from calculation_checks import arithmetic_report, example_issues

EXAMPLE = {'id':'E1','kind':'calculation','source_page':15,
           'quote':'e=(a+b)*(b+1)，a=2，b=1','steps':['e=6','de/da=2','de/db=2+3=5'],
           'result':{'variable':'e','value':6,'gradients':{'a':2,'b':5}}}


class TeachingChecks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=ROOT/'jobs/_validation',prefix='teaching-generation-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.slides = self.root/'slides'; self.slides.mkdir()
        for i in range(3):
            (self.slides/f'{i+1}.png').write_bytes(f'image-{i}'.encode())
        self.prompt = self.root/'prompt.txt'; self.prompt.write_text('只输出讲稿 | no',encoding='utf-8')
        self.topics = ['参数更新\n学习率与梯度','链式法则\n单路径相乘','总结\n分支相加']
        self.policy = {'script_lang':'zh','speech_rate':220,'examples':[]}

    def generate(self, model, **kwargs):
        with patch.object(course,'slide_topics',return_value=self.topics):
            return course.generate_script(None,self.slides,self.prompt,{},self.root,model,
                                          teaching=self.policy,workers=3,**kwargs)

    def test_actual_previous_speech_and_future_scope_are_ordered(self):
        calls=[]
        def model(*args,**kw):
            calls.append(kw['prompt_text'])
            return f'第{len(calls)}步实际讲过的独特内容。 | no',{'tokens':1}
        text,_ = self.generate(model)
        self.assertEqual(len(calls),3)
        self.assertNotIn('第1步实际讲过的独特内容',calls[0])
        self.assertIn('第1步实际讲过的独特内容',calls[1])
        self.assertIn('第2步实际讲过的独特内容',calls[2])
        self.assertIn('本页任务',calls[1]); self.assertIn('后面安排的内容',calls[0])
        self.assertIn('链式法则',calls[0])
        self.assertEqual(len(course.split_pages(text)),3)
        lesson=course.read_json(self.root/'teaching_plan.json')
        self.assertEqual(lesson['pages'][2]['role'],'回顾检查')
        self.assertIn('后面要承接的问题：'+lesson['pages'][0]['handoff_question'],calls[0])
        self.assertIn(lesson['pages'][1]['guiding_question'],calls[1])
        self.generate(model)
        self.assertEqual(len(calls),3,'相同前页上下文应全部命中缓存')

    def test_rewrite_binds_actual_earlier_manual_edits_and_preserves_other_pages(self):
        calls=[]
        def model(*args,**kw):
            calls.append(kw['prompt_text']); return '本页根据前文继续解释。 | no',{}
        text,_=self.generate(model)
        course.save_script(self.root/'subtitle_w_cursor.txt',text)
        self.generate(model,selected=[1],generation_id='rewrite-one')
        self.assertEqual(len(calls),4)
        self.generate(model,selected=[1],generation_id='rewrite-one')
        self.assertEqual(len(calls),4)
        pages=course.split_pages((self.root/'subtitle_w_cursor.txt').read_text('utf-8'))
        pages[0]='人工修改的先备知识。 | no'
        course.save_script(self.root/'subtitle_w_cursor.txt','\n###\n'.join(pages))
        self.generate(model,selected=[1],generation_id='rewrite-one')
        self.assertEqual(len(calls),5,'前页变化必须使同次重写缓存失效')
        self.assertIn('人工修改的先备知识',calls[-1])
        saved=course.split_pages((self.root/'subtitle_w_cursor.txt').read_text('utf-8'))
        self.assertEqual(saved[0],pages[0]); self.assertEqual(saved[2],pages[2])
        self.assertTrue(any('第3页' in warning for warning in course.teaching_review(saved,self.root,self.policy)['warnings']))

    def test_correction_fixes_math_and_persists_candidate_history(self):
        self.slides.joinpath('2.png').unlink(); self.slides.joinpath('3.png').unlink()
        self.topics=['例题 E1\n计算图']
        self.policy['examples']=[EXAMPLE]
        calls=[]
        def model(*args,**kw):
            calls.append(kw['prompt_text'])
            answer='4' if len(calls)==1 else '2+3=5'
            return f'e=6，∂e/∂a=2，∂e/∂b={answer}。 | no',{'tokens':1}
        text,_=self.generate(model)
        self.assertEqual(len(calls),2); self.assertIn('正确值5',calls[1])
        self.assertEqual(example_issues(course.spoken_text(text),EXAMPLE),[])
        saved=course.read_json(self.root/'course_script/0.json')
        self.assertEqual(len(saved['history']),2); self.assertTrue(saved['complete'])

    def test_interrupted_repair_resumes_without_repaying_initial_generation(self):
        self.slides.joinpath('2.png').unlink(); self.slides.joinpath('3.png').unlink()
        self.topics=['例题 E1']; self.policy['examples']=[EXAMPLE]
        calls=[]
        def model(*args,**kw):
            calls.append(kw['prompt_text'])
            if len(calls)==2: raise RuntimeError('仅测试中断')
            return 'e=6，∂e/∂a=2，∂e/∂b=4。 | no',{}
        with self.assertRaises(RuntimeError): self.generate(model)
        self.assertEqual(len(course.read_json(self.root/'course_script/0.json')['history']),1)
        self.generate(model)
        self.assertEqual(len(calls),4,'仅剩余两次修正，初稿不重发')
        self.assertIn('上一版讲稿',calls[2])
        self.generate(model)
        self.assertEqual(len(calls),4,'耗尽修正额度后不能自动继续付费')

    def test_chained_and_spoken_results_and_contradictions(self):
        good=('e=3*2=6，∂e/∂a=2*1=2，∂e/∂b=2+3=5。',
              'e等于六；e对a的偏导为二；e对b的偏导等于五。',
              'e等于c乘d，结果是6。de/da=2。总偏导de/db是两路贡献之和，2加3等于5。',
              'e=6，de/da=2。而b不同：它既影响c，又影响d。从c路径贡献2，从d路径贡献3，总和是5。')
        for text in good:
            with self.subTest(text=text): self.assertEqual(example_issues(text,EXAMPLE),[])
        for text in ('e=60，de/da=2，de/db=5。','e=6，de/da=2，de/db=2+3=6。','e=6，de/da=2，de/db=5；de/db=4。'):
            self.assertTrue(example_issues(text,EXAMPLE))

    def test_numeric_supplement_is_recalculated_and_code_never_executed(self):
        bad='举个教学小例：若J=(w-3)^2，当前w=5，则dJ/dw=4；取eta=0.1，w_new=5-0.1*4=4.6，损失从4降至1.96。'
        result=arithmetic_report(bad)
        self.assertTrue(any('2.56' in s for s in result['issues']))
        self.assertFalse(arithmetic_report(bad.replace('1.96','2.56'))['issues'])
        self.assertTrue(arithmetic_report(bad.replace('dJ/dw=4','dJ/dw=3'))['issues'])
        self.assertTrue(any('更新值' in s for s in arithmetic_report(bad.replace('5-0.1*4=4.6','4.7'))['issues']))
        self.assertTrue(arithmetic_report('两路贡献2*1+3*1=6。')['issues'])
        self.assertFalse(arithmetic_report('两路贡献2*1+3*1=5。')['issues'])
        self.assertFalse(arithmetic_report('计算1/3=0.333333。')['issues'])
        self.assertTrue(arithmetic_report('计算1/3=0.34。')['issues'])
        for expression in ('__import__("os").system("x")','(1).__class__','2**100000','lambda: 3','sin(1,2)'):
            with self.assertRaises((ValueError,SyntaxError)):
                teaching_quality._calculate(expression,{})

    @unittest.skipUnless((ROOT/'jobs/_validation/nlp_teaching_2026-10-15/common_slides.pdf').is_file(),'本地冻结实验材料未提供')
    def test_frozen_six_scripts_have_no_example_false_positives_and_c2_error_is_detected(self):
        import pymupdf
        frozen=ROOT/'jobs/_validation/nlp_teaching_2026-10-15'
        with pymupdf.open(frozen/'common_slides.pdf') as pdf:
            topics=[page.get_text() for page in pdf]
        plan=course.read_json(frozen/'plan.json')
        for label in ('A1','B1','C1','A2','B2','C2'):
            raw=(frozen/label/'subtitle_w_cursor.txt').read_text('utf-8')
            report=teaching_quality.script_report(course.split_pages(raw),plan,topics)
            self.assertFalse(any('例题E1' in s for s in report['issues']),(label,report['issues']))
            if label=='C2': self.assertTrue(any('2.56' in s for s in report['issues']))

    def test_example_weighted_time_keeps_section_totals_and_reports_local_overrun(self):
        plan,_=course.build_plan('材料。',{'duration_mode':'custom','target_minutes':4},4,'zh')
        topics=['引入','概念','例题 E1','总结']
        plan['examples']=[EXAMPLE]
        allocations=course.page_sections(plan,4,topics)
        self.assertAlmostEqual(sum(s['page_minutes'] for s in allocations),4)
        self.assertGreater(allocations[2]['page_minutes'],allocations[1]['page_minutes'])
        pages=['字'*round(s['page_minutes']*220)+'。 | no' for s in allocations]
        # 保持总长度不变，但将两页预算互换，应检出单页失配。
        pages[0],pages[2]=pages[2],pages[0]
        plan['examples']=[]
        report=teaching_quality.script_report(pages,plan,topics)
        self.assertTrue(any('第1页' in s and '预算' in s for s in report['issues']))

    def audio(self, seconds, cache_seconds=None):
        audio=self.root/'audio'; audio.mkdir(exist_ok=True)
        script='字'*220+'。 | no'
        course.save_script(self.root/'subtitle_w_cursor.txt',script)
        filename=audio/'0.wav'
        with wave.open(str(filename),'wb') as wav:
            wav.setparams((1,2,1000,0,'NONE','not compressed')); wav.writeframes(bytes(round(seconds*1000)*2))
        course.write_json(audio/'manifest.json',{'0':{'text_sha':course.digest(course.spoken_text(script)),
            'wav_sha':course.digest(filename.read_bytes()),'voice_identity':'test-voice',
            'seconds':cache_seconds if cache_seconds is not None else seconds}})
        return {'sections':[{'id':'all','title':'教材','minutes':1}],'script_lang':'zh','speech_rate':220,'target_minutes':1}

    def test_measured_audio_blocks_overrun_and_ignores_incorrect_manifest_duration(self):
        plan=self.audio(80,cache_seconds=60)
        report=course.audio_report(plan,self.root)
        self.assertAlmostEqual(report['actual_minutes'],1.33,places=2)
        self.assertFalse(report['actual_passed'])
        with self.assertRaisesRegex(ValueError,'合片前验收'): course.require_audio_timing(plan,self.root)
        self.assertTrue((self.root/'audio/0.wav').exists(),'超时不能删除已付费音频')
        self.assertFalse(course.read_json(self.root/'course_timing.json')['actual_passed'])

    def test_matching_audio_passes_but_edit_missing_or_corruption_invalidates_timing(self):
        plan=self.audio(60)
        self.assertTrue(course.require_audio_timing(plan,self.root)['actual_passed'])
        for kind in ('edit','corrupt','missing'):
            plan=self.audio(60)
            if kind=='edit': course.save_script(self.root/'subtitle_w_cursor.txt','修改口播。 | no')
            elif kind=='corrupt': (self.root/'audio/0.wav').write_bytes(b'bad')
            else: (self.root/'audio/0.wav').unlink()
            with self.subTest(kind=kind):
                report=course.audio_report(plan,self.root)
                self.assertIsNone(report['actual_minutes'])
                with self.assertRaises(ValueError): course.require_audio_timing(plan,self.root)

    def test_actual_speech_rate_calibrates_next_revision_and_voice_changes_reset_it(self):
        plan=self.audio(80)
        with self.assertRaises(ValueError): course.require_audio_timing(plan,self.root)
        calibrated=course.timing_policy(plan,self.root)
        self.assertEqual(calibrated['speech_rate'],165)
        self.assertEqual(plan['speech_rate'],220,'不得改写用户原教案')
        shorter='字'*165+'。 | no'
        course.save_script(self.root/'subtitle_w_cursor.txt',shorter)
        self.assertTrue(course.teaching_review([shorter],self.root,plan)['passed'])
        self.assertEqual(course.audio_report(plan,self.root)['estimate_speech_rate'],165)
        manifest=course.read_json(self.root/'audio/manifest.json')
        manifest['0']['voice_identity']='different-voice-or-parameters'
        course.write_json(self.root/'audio/manifest.json',manifest)
        self.assertEqual(course.timing_policy(plan,self.root)['speech_rate'],220)


if __name__=='__main__':
    unittest.main(verbosity=2)
