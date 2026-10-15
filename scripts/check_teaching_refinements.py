"""实测缺陷离线回归；冻结证据只读，不调用模型或云配音。"""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT/'Paper2Video-main/src')]
import course
import bailian_speech as speech
from calculation_checks import claims, example_issues, arithmetic_report
from lesson_planning import page_tasks, lesson_report, context_prompt
from script_revision import oral_fingerprint, paragraph_targets
from spoken_text import prepare_spoken, plain_text, NORMALIZATION_VERSION
from tensor_checks import tensor_contracts, tensor_report, matmul, broadcast

SHAPE_EXAMPLE = {'id':'E2','kind':'shape','source_page':15,
                 'quote':'按代码x[1,10]乘W[10,5]，加b[5]后逐元素sigmoid。','steps':[]}
VALUE_EXAMPLE = {'id':'E1','kind':'calculation','source_page':9,
                 'result':{'variable':'z','value':20,'gradients':{}}}


class RefinementChecks(unittest.TestCase):
    def test_oral_fingerprint_ignores_cursor_spacing_and_numeric_format(self):
        self.assertEqual(oral_fingerprint('z=20。 | no'),oral_fingerprint('z 等于二十！ | "z"'))
        self.assertNotEqual(oral_fingerprint('z=20。 | no'),oral_fingerprint('z=21。 | no'))

    def test_paragraph_targets_preserve_reasoning_priority_and_total(self):
        rows = paragraph_targets('铺垫'*100+'。 | no\n因为条件成立所以结果等于二十。'+'推导'*91+'。 | no',1,200)
        self.assertEqual(sum(r['target_units'] for r in rows),200)
        self.assertGreater(rows[1]['target_units'],rows[0]['target_units'])
        self.assertTrue(rows[1]['preserve_reasoning'])
        self.assertTrue(all(0<=r['target_units']<=r['units'] for r in rows))

    def test_no_change_and_cycle_stop_persist_and_resume_without_calls(self):
        for outputs, expected, reason in ((['长'*400+'。 | no','长'*400+'！ | "目标"'],2,'no_oral_change'),
                                           (['甲'*400+'。 | no','乙'*400+'。 | no','甲'*400+'。 | no'],3,'repeated_oral_content')):
            with self.subTest(reason=reason), tempfile.TemporaryDirectory(dir=ROOT/'jobs/_validation') as directory:
                root=Path(directory); slides=root/'slides'; slides.mkdir(); (slides/'1.png').write_bytes(b'fixture')
                prompt=root/'prompt.txt'; prompt.write_text('严格输出 | no',encoding='utf-8')
                plan,_=course.build_plan('教材。',{'duration_mode':'custom','target_minutes':1,'speech_rate':200},4,'zh')
                plan['slide_count']=1; plan['sections'][0].update(first_page=1,last_page=1)
                calls=[]
                def model(*args,**kwargs):
                    calls.append(kwargs['prompt_text']); return outputs[len(calls)-1],{'tokens':1}
                args=(plan,slides,prompt,{},root,model)
                text,_=course.generate_script(*args,workers=1)
                saved=course.read_json(root/'course_script/0.json')
                self.assertEqual(len(calls),expected); self.assertEqual(saved['stop_reason'],reason)
                self.assertTrue(saved['needs_review']); self.assertTrue(saved['history'][0]['paragraph_targets'])
                self.assertIn('逐段删减目标',calls[1])
                course.generate_script(*args,workers=1)
                self.assertEqual(len(calls),expected)
                report=course.teaching_review([text],root,plan)
                self.assertFalse(report['passed']); self.assertEqual(report['revision_stops'][0]['reason'],reason)

    def test_ledger_keeps_evidence_and_explicit_question_responsibility(self):
        tasks=page_tasks(['引入\n问题：为什么尚无结果？\n本课路线：计算图到会话','会话\n按依赖执行','总结\n检查：是否已经运行？'])
        spoken=['为什么尚无结果？下一页解释会话如何按依赖执行。','按依赖执行。']
        report=lesson_report(tasks,spoken)
        self.assertEqual(report['pending_questions'][0]['answer_page'],2)
        self.assertEqual(report['pending_questions'][0]['status'],'到页待核对回答')
        self.assertIsNotNone(report['pages'][1]['knowledge'][0]['evidence'])
        self.assertEqual(report['pages'][1]['question_status'],'待核对回答')
        self.assertIn('P2.K1',context_prompt(tasks,1,spoken[:1]))
        self.assertIn('实际已讲知识证据',context_prompt(tasks,1,spoken[:1]))
        self.assertEqual(tasks[0]['role'],'问题引入')

    def test_repeated_opening_and_future_result_but_not_one_short_bridge(self):
        tasks=page_tasks(['引入\n提出问题','例题 E1\n计算z','概念\n另一个问题'],[VALUE_EXAMPLE])
        opening='我们刚刚写出一个表达式但是屏幕上并没有数字为什么定义了表达式还没有拿到结果？'
        self.assertTrue(lesson_report(tasks,[opening,opening],[VALUE_EXAMPLE])['issues'])
        self.assertTrue(any('提前' in i for i in lesson_report(tasks,['先算z=20。'],[VALUE_EXAMPLE])['issues']))
        bridge='前面已经说明计算图只定义依赖关系需要执行才能得到输出数值。'
        self.assertFalse(lesson_report(tasks,['开场。'+bridge,'现在继续。'+bridge+'本页解释新的知识。'],[])['issues'])

    def test_tensor_order_output_and_reverse_reason(self):
        contracts=tensor_contracts([SHAPE_EXAMPLE])
        self.assertEqual(contracts[0]['symbols']['x'],(1,10))
        self.assertEqual(matmul((1,10),(10,5)),(1,5))
        for text in ('x乘W得到[1,5]。','如果误写成tf.matmul(W,x)，就会因内维5≠1报错。'):
            self.assertFalse(tensor_report(text,contracts)['issues'],text)
        for text in ('h=sigmoid(W*x+b)。','x乘W得到[10,5]。',
                     '如果误写成tf.matmul(W,x)，就会因内维10≠5报错。',
                     '若误写成tf.matmul(W,x)，shape将因内维5≠10而报错。'):
            self.assertTrue(tensor_report(text,contracts)['issues'],text)
        report=tensor_report('tf.matmul(A,B)。',contracts)
        self.assertTrue(report['uncovered'])
        self.assertFalse(any(c['kind']=='spoken_matmul' for c in report['checks']))
        other={'id':'E3','kind':'shape','source_page':20,'quote':'x[2,3]乘W[3,4]','steps':[]}
        scoped=tensor_contracts([SHAPE_EXAMPLE,other])
        self.assertTrue(tensor_report('x乘W得到[2,4]。',scoped)['uncovered'])
        self.assertFalse(tensor_report('x乘W得到[2,4]。',scoped,['E3'])['issues'])

    def test_broadcast_valid_expansion_is_distinct_from_runtime_error(self):
        contracts=tensor_contracts([SHAPE_EXAMPLE])
        self.assertEqual(broadcast((1,5),(5,1)),(5,5))
        for text in ('偏置b[5]广播后仍为[1,5]。','偏置b[5,1]广播后得到[5,5]。'):
            self.assertFalse(tensor_report(text,contracts)['issues'],text)
        for text in ('偏置b[5,1]广播后仍为[1,5]。','若误用[5,1]就会报错。','广播只作用于末轴。'):
            self.assertTrue(tensor_report(text,contracts)['issues'],text)
        with self.assertRaises(ValueError): broadcast((2,5),(3,5))

    def test_result_suffix_oral_chain_and_contradiction(self):
        for text in ('这个z=20的Tensor有什么属性？','先算z：5乘4等于20。','z=5*4=20。'):
            self.assertFalse(example_issues(text,VALUE_EXAMPLE),text)
        self.assertTrue(example_issues('z=20的Tensor，然而z=30。',VALUE_EXAMPLE))
        self.assertEqual(claims('z=20乘以x。','z'),[])
        self.assertTrue(arithmetic_report('z=d*c。')['uncovered'])

    def test_code_identifier_mapping_and_strict_timestamp_validation(self):
        text='tf.Variable、tf.nn.sigmoid和feed_dict，3.14。'
        spoken,mapping=prepare_spoken(text)
        self.assertEqual(spoken,'tf点Variable、tf点nn点sigmoid和feed下划线dict，三点一四。')
        self.assertEqual(''.join(m['normalized'] for m in mapping),plain_text(spoken))
        dot=mapping[2]; self.assertEqual(text[dot['original_index']:dot['original_end']],'.')
        timing={'segments':[{'words':[{'word':spoken,'start':0,'end':1}]}],
                'spoken_text':spoken,'spoken_sha256':speech.digest(spoken),'normalization_map':mapping,
                'normalization_version':NORMALIZATION_VERSION,'normalization_language':'zh'}
        speech.validate_timing(timing,bytes(48000),text)
        for bad in ('tfVariable、tf点nn点sigmoid和feed下划线dict，三点一四。',spoken+'点'):
            with self.assertRaises(ValueError):
                speech.validate_timing({**timing,'segments':[{'words':[{'word':bad,'start':0,'end':1}]}]},bytes(48000),text)
        with self.assertRaises(ValueError): speech.validate_timing({**timing,'normalization_version':1},bytes(48000),text)
        self.assertEqual(prepare_spoken('tf.Variable', 'en')[0],'tf.Variable')
        self.assertEqual(prepare_spoken('w_1')[0],'w下标一')

    def test_unknown_submission_blocks_normalization_version_change(self):
        from check_bailian_speech import SpeechTests, PCM
        calls=[]
        class Broken:
            def synthesize(self,*args): calls.append(1); raise ValueError('结果未知')
        with tempfile.TemporaryDirectory(dir=ROOT/'jobs/_validation') as directory:
            with patch.object(speech,'NORMALIZATION_VERSION',1), self.assertRaises(ValueError):
                speech.tts_pages([[('tf.Variable','no')]],directory,SpeechTests().snapshot(),client=Broken())
            with self.assertRaisesRegex(ValueError,'未知'):
                speech.tts_pages([[('tf.Variable','no')]],directory,SpeechTests().snapshot(),client=Broken())
        self.assertEqual(len(calls),1)

    def test_frozen_chapter_three_counterexamples_read_only(self):
        root=ROOT/'jobs/_validation/nlp_ch3_2026-10-15'
        if not root.is_dir(): self.skipTest('本机冻结证据未保存')
        plan=json.loads((root/'plan.json').read_text('utf-8'))
        contracts=tensor_contracts(plan['examples'])
        self.assertEqual(contracts[0]['matmul'][0]['output'],(1,5))
        # 原失败诊断只读，不把它改写为新版本成功记录。
        diagnostic=json.loads((root/'tts_diagnostics/3f5fbba90cfa43df9dd92ea6b07ad023.json').read_text('utf-8'))
        self.assertEqual(diagnostic['state'],'mapping_failed')
        from teaching_quality import script_report
        topics=json.loads((root/'topics.json').read_text('utf-8'))
        results={}
        for label in ('A1','A2','B1','B2'):
            pages=course.split_pages((root/label/'subtitle_w_cursor.txt').read_text('utf-8'))
            results[label]=script_report(pages,plan,topics)
            self.assertFalse(any('例题E1未完整兑现' in issue for issue in results[label]['issues']),label)
        self.assertTrue(any('重复前文' in issue for issue in results['B1']['issues']))
        self.assertTrue(any('实际内维是5与1' in issue for issue in results['B1']['issues']))
        self.assertTrue(any('实际内维是5与1' in issue for issue in results['B2']['issues']))


if __name__=='__main__': unittest.main()
