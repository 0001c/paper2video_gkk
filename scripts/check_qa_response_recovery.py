"""问答根格式、证据修正恢复与安全诊断回归；不调用模型或加载权重。"""
import copy
import json
from pathlib import Path
import runpy
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'Paper2Video-main/src')]
import qa_evaluation as qa
import quality_evaluation as quality
from web.model_config import EvaluationPolicy, PipelineModelConfig, legacy_config


class ResponseRecoveryChecks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=ROOT / 'jobs/_validation', prefix='qa-response-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'source.md'
        self.source.write_text('原文知识', encoding='utf-8')
        (self.root / 'subtitle_w_cursor.txt').write_text('原文知识 | no', encoding='utf-8')
        self.bank = {'id': 'fixture', 'source_sha256': qa.digest_file(self.source), 'questions': [
            {'id': str(i + 1), 'question': f'问题{i + 1}', 'options': dict(A='原文知识', B='乙', C='丙', D='丁'),
             'answer': 'A', 'kind': 'detail', 'source_quote': '原文知识'} for i in range(4)]}
        self.good = [{'id': str(i + 1), 'answer': 'A', 'evidence_quotes': ['原文知识']} for i in range(4)]
        self.bad = [{**answer, 'evidence_quotes': ['材料不存在的引文']} for answer in self.good]
        self.validate = lambda answers: qa.grade(self.bank, answers, '原文知识')
        value = legacy_config({}, 'fixture').public_dict()
        value['stages']['quiz'] = {**copy.deepcopy(value['stages']['text']), 'model': 'quiz-model'}
        value['stages']['qa'] = {**copy.deepcopy(value['stages']['vision']), 'model': 'qa-model'}
        value['evaluation'] = EvaluationPolicy(enabled=True, question_count=4,
            checks={name: {'enabled': name == 'script', 'threshold': 80}
                    for name in ('script', 'video', 'slides', 'presentation', 'audio')}).model_dump()
        self.config = PipelineModelConfig.model_validate(value)
        self.output = self.root / 'report.json'

    def evaluate(self, request):
        with patch.object(quality, 'role_settings', return_value={'model_type': 'fixture'}), \
                patch.object(quality, 'question_bank', return_value=self.bank):
            return quality.evaluate_checks({}, self.config, self.source, self.root, self.output,
                                           'script', 'zh', request)

    def test_null_and_list_roots_retry_and_preserve_full_response(self):
        request = MagicMock(side_effect=[None, [], {'answers': self.good, 'extra': '完整根响应'}])
        target = self.root / 'answers.response.json'
        answers = qa.request_answers({}, [{'type': 'text', 'text': '材料'}], self.root, target, request,
                                     validate=self.validate)
        self.assertEqual(request.call_count, 3)
        self.assertEqual(qa.grade(self.bank, answers, '原文知识')['score'], 100)
        self.assertEqual(qa.read_json(target.with_suffix('.root.json'))['extra'], '完整根响应')
        self.assertEqual(len(list(self.root.glob('*invalid-root*.json'))), 2)

    def test_invalid_root_attempt_limit_never_fabricates_answers(self):
        request = MagicMock(return_value=None)
        target = self.root / 'answers.response.json'
        with self.assertRaisesRegex(ValueError, 'JSON对象'):
            qa.request_answers({}, '材料', self.root, target, request, validate=self.validate)
        self.assertEqual(request.call_count, qa.ANSWER_FORMAT_ATTEMPTS)
        self.assertFalse(target.exists())

    def test_legal_object_with_invalid_field_still_uses_grade_validation(self):
        request = MagicMock(return_value={'answers': None})
        with self.assertRaisesRegex(ValueError, 'answers数组'):
            qa.request_answers({}, '材料', self.root, self.root / 'answers.json', request, validate=self.validate)
        self.assertEqual(request.call_count, 1)
        self.assertFalse((self.root / 'answers.json').exists())
        retry = MagicMock(return_value={'answers': self.good})
        self.assertEqual(qa.request_answers({}, '材料', self.root, self.root / 'answers.json', retry,
                                          validate=self.validate), self.good)
        self.assertEqual(retry.call_count, 1, '无效根旁路文件不能锁死后续重评')

    def test_existing_first_answer_cache_needs_no_model_call(self):
        target = self.root / 'answers.response.json'
        qa.write_json(target, self.good)
        request = MagicMock(side_effect=AssertionError('不应重付答题费用'))
        self.assertEqual(qa.request_answers({}, '材料', self.root, target, request, validate=self.validate), self.good)
        request.assert_not_called()

    def test_invalid_existing_answer_cache_is_preserved_but_can_be_replaced(self):
        target = self.root / 'answers.response.json'
        invalid = [{'id': '1', 'answer': 'Z'}]
        qa.write_json(target, invalid)
        request = MagicMock(return_value={'answers': self.good})
        self.assertEqual(qa.request_answers({}, '材料', self.root, target, request, validate=self.validate), self.good)
        self.assertEqual(request.call_count, 1)
        self.assertEqual(qa.read_json(target.with_suffix('.invalid-cache.json')), invalid)

    def test_failed_repair_resumes_only_repair_after_successful_first_answers(self):
        request = MagicMock(side_effect=[{'answers': self.bad}, None, [], None])
        with self.assertRaisesRegex(ValueError, '证据修正模型未返回JSON对象'):
            self.evaluate(request)
        self.assertEqual(request.call_count, 4)
        self.assertFalse(self.output.exists())
        self.assertEqual(qa.read_json(next((self.root / 'evaluation').glob('knowledge-*.response.json'))), self.bad)
        retry = MagicMock(return_value={'answers': self.good})
        report = self.evaluate(retry)
        self.assertEqual(retry.call_count, 1)
        self.assertIn('证据格式修正', retry.call_args.args[1][0]['text'])
        self.assertEqual(report['score'], 100)
        self.assertTrue(report['checks']['script']['evidence_format_repair'])

    def test_completed_repair_response_survives_interruption_before_score_cache(self):
        request = MagicMock(side_effect=[{'answers': self.bad}, {'answers': self.good}])
        real_write = quality.write_json
        def interrupted(path, value):
            if Path(path).name.startswith('knowledge-') and '.response' not in Path(path).name:
                raise OSError('模拟发布评分前中断')
            return real_write(path, value)
        with patch.object(quality, 'write_json', side_effect=interrupted):
            with self.assertRaises(OSError):
                self.evaluate(request)
        self.assertEqual(request.call_count, 2)
        self.assertFalse(self.output.exists())
        retry = MagicMock(side_effect=AssertionError('首轮和修正均应复用'))
        self.assertEqual(self.evaluate(retry)['score'], 100)
        retry.assert_not_called()

    def test_cached_repair_cannot_change_answers(self):
        changed = [{**answer, 'answer': 'B'} for answer in self.good]
        request = MagicMock(side_effect=[{'answers': self.bad}, {'answers': changed}])
        with self.assertRaisesRegex(ValueError, '改变了答题选项'):
            self.evaluate(request)
        self.assertFalse(self.output.exists())
        first = next((self.root / 'evaluation').glob('knowledge-*.response.json'))
        repair = first.with_name(first.name.replace('.response.json', '.repair-response.json'))
        self.assertFalse(repair.exists(), '改选项的响应不能存为成功缓存')
        qa.write_json(repair, changed)  # 兼容此前留下的无效修正缓存。
        retry = MagicMock(return_value={'answers': self.good})
        report = self.evaluate(retry)
        self.assertEqual(retry.call_count, 1)
        self.assertEqual(report['score'], 100)
        self.assertTrue(repair.with_suffix('.invalid-cache.json').exists())

    def test_legacy_path_also_retries_invalid_answer_roots(self):
        value = self.config.public_dict()
        value['evaluation'] = EvaluationPolicy(enabled=True, question_count=4).model_dump()
        legacy = PipelineModelConfig.model_validate(value)
        request = MagicMock(side_effect=[[], None, {'answers': self.good}])
        with patch.object(qa, 'load_model_snapshot', return_value=({}, legacy)), \
                patch.object(qa, 'role_settings', return_value={'model_type': 'fixture'}), \
                patch.object(qa, 'question_bank', return_value=self.bank):
            report = qa.evaluate('script', 'unused', self.source, self.root, self.output, request=request)
        self.assertEqual(request.call_count, 3)
        self.assertEqual(report['score'], 100)

    def test_question_bank_retries_root_format_without_changing_question_rules(self):
        request = MagicMock(side_effect=[None, [], {'questions': self.bank['questions']}])
        bank = qa.question_bank(self.source, self.root / 'bank', {'model_type': 'fixture'}, 4, request)
        self.assertEqual(request.call_count, 3)
        self.assertEqual(len(bank['questions']), 4)
        self.assertTrue(list((self.root / 'bank').glob('*.response.json')))

    def test_failed_question_root_does_not_block_future_regeneration(self):
        bank_root = self.root / 'bank'
        request = MagicMock(return_value=[])
        with self.assertRaisesRegex(ValueError, 'JSON对象'):
            qa.question_bank(self.source, bank_root, {'model_type': 'fixture'}, 4, request)
        self.assertEqual(request.call_count, 3)
        retry = MagicMock(return_value={'questions': self.bank['questions']})
        bank = qa.question_bank(self.source, bank_root, {'model_type': 'fixture'}, 4, retry)
        self.assertEqual(retry.call_count, 1)
        self.assertEqual(len(bank['questions']), 4)

    def test_rubric_root_format_keeps_existing_total_attempt_limit(self):
        page = {'page': 1, 'start': 0, 'end': 1, 'frames': []}
        good = {'checks': {'slides': {'criteria': {name: 100 for name in quality.CRITERIA['slides']}, 'issues': []}}}
        request = MagicMock(side_effect=[None, [], good])
        target = self.root / 'rubric.json'
        result = quality.rubric_page({}, [{'type': 'text', 'text': '材料'}], self.root,
                                    target, ['slides'], page, '', '', request)
        self.assertEqual(request.call_count, quality.RUBRIC_ATTEMPTS)
        self.assertEqual(result['slides']['score'], 100)
        self.assertEqual(qa.read_json(target.with_suffix('.root.json')), good)

    def audio_response(self):
        return {'checks': {
            'slides': {'criteria': {name: 100 for name in quality.CRITERIA['slides']}, 'issues': []},
            'presentation': {'criteria': {name: 100 for name in quality.CRITERIA['presentation']}, 'issues': []},
            'audio': {'criteria': {'coverage': 100, 'accuracy': 95, 'order': 100}, 'issues': [{
                'criterion': 'accuracy',
                'description': 'ASR识别结果与讲稿存在差异，将“喂给”识别为“未给”，属于同音字错误，需人工复核确认实际发音。',
                'evidence': '', 'timestamp': None,
                'script_quote': '代表了我们喂给模型多少文本让它去阅读和学习',
                'asr_quote': '代表了我们未给模型多少文本，让它去阅读和学习'}]}}}

    def test_empty_audio_evidence_uses_verified_quotes_and_preserves_score_and_review(self):
        response = self.audio_response()
        audio = response['checks']['audio']
        issue = audio['issues'][0]
        page = {'page': 9, 'start': 600, 'end': 675, 'frames': []}
        row = quality.rubric_result('audio', audio, page, issue['script_quote'], issue['asr_quote'])
        self.assertEqual(row['score'], 95)
        self.assertEqual(row['criteria'], audio['criteria'])
        self.assertTrue(row['issues'][0]['requires_review'])
        self.assertEqual(row['issues'][0]['description'], issue['description'])
        for name in ('script_quote', 'asr_quote'):
            self.assertIn(issue[name], row['issues'][0]['evidence'])
        self.assertEqual(issue['evidence'], '', '原模型响应保持原样，展示文本只在验证后产生')

    def test_empty_evidence_does_not_allow_unmatched_quotes_missing_description_or_visual_evidence(self):
        page = {'page': 9, 'start': 600, 'end': 675, 'frames': [{'timestamp': 640}]}
        audio = self.audio_response()['checks']['audio']
        issue = audio['issues'][0]
        with self.assertRaisesRegex(ValueError, '引文未匹配'):
            quality.rubric_result('audio', audio, page, '捏造的讲稿内容', issue['asr_quote'])
        for field, invalid in (('description', ''), ('evidence', None), ('evidence', ' ')):
            bad = copy.deepcopy(audio)
            bad['issues'][0][field] = invalid
            with self.subTest(field=field, invalid=invalid), self.assertRaisesRegex(ValueError, '具体说明与证据'):
                quality.rubric_result('audio', bad, page, issue['script_quote'], issue['asr_quote'])
        visual = {'criteria': {name: 95 for name in quality.CRITERIA['slides']}, 'issues': [{
            'criterion': 'layout', 'description': '版面拥挤', 'evidence': '', 'timestamp': 640}]}
        with self.assertRaisesRegex(ValueError, '具体说明与证据'):
            quality.rubric_result('slides', visual, page, '', '')

    def test_paid_audio_response_is_revalidated_without_requesting_the_page_again(self):
        response = self.audio_response()['checks']
        audio = response['audio']['issues'][0]
        page = {'page': 9, 'start': 600, 'end': 675, 'frames': []}
        target = self.root / 'rubric-page-9.json'
        qa.write_json(target.with_suffix('.response.json'), response)
        request = MagicMock(side_effect=AssertionError('已有完整引文，不应重复付费评价'))
        rows = quality.rubric_page({}, [{'type': 'text', 'text': '材料'}], self.root, target,
            ['slides', 'presentation', 'audio'], page, audio['script_quote'], audio['asr_quote'], request)
        request.assert_not_called()
        self.assertEqual(rows['audio']['score'], 95)
        self.assertEqual(qa.read_json(target), response)

    def test_invalid_paid_response_returns_to_existing_page_retry_budget(self):
        target = self.root / 'rubric-page.json'
        qa.write_json(target.with_suffix('.response.json'), {'slides': {'criteria': {}, 'issues': []}})
        good = {'checks': {'slides': {'criteria': {name: 100 for name in quality.CRITERIA['slides']}, 'issues': []}}}
        request = MagicMock(return_value=good)
        rows = quality.rubric_page({}, [{'type': 'text', 'text': '材料'}], self.root, target, ['slides'],
            {'page': 1, 'start': 0, 'end': 1, 'frames': []}, '', '', request)
        self.assertEqual(request.call_count, 1)
        self.assertEqual(rows['slides']['score'], 100)

    def test_cli_diagnostics_store_safe_call_locations_without_exception_text(self):
        output = self.root / 'qa.json'
        source_file = ROOT / 'Paper2Video-main/src/qa_evaluation.py'
        argv = [str(source_file), '--snapshot', 'unused', '--source', str(self.source),
                '--result', str(self.root), '--output', str(output), '--mode', 'script']
        with patch.object(sys, 'argv', argv), \
                patch('model_runtime.load_model_snapshot', side_effect=AttributeError('sk-private-exception')):
            with self.assertRaises(SystemExit):
                runpy.run_path(str(source_file), run_name='__main__')
        diagnostic = qa.read_json(str(output) + '.diagnostic.json')
        self.assertEqual(diagnostic['type'], 'AttributeError')
        self.assertTrue(any(frame['function'] == 'evaluate' for frame in diagnostic['frames']))
        self.assertTrue(all(set(frame) == {'file', 'function', 'line'} for frame in diagnostic['frames']))
        self.assertTrue(all('/' not in frame['file'] and '\\' not in frame['file'] for frame in diagnostic['frames']))
        failure = qa.read_json(str(output) + '.error.json')
        self.assertNotIn('sk-private-exception', json.dumps([failure, diagnostic]))


if __name__ == '__main__':
    unittest.main()
