"""自定义时长的有界修正与缓存恢复回归，不调用真实模型。"""
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'Paper2Video-main/src'))
import course


class ScriptBudgetChecks(unittest.TestCase):
    def generate(self, outputs, mode='custom'):
        self.work = tempfile.TemporaryDirectory(dir=ROOT / 'jobs/_validation', prefix='script-budget-')
        self.addCleanup(self.work.cleanup)
        root = Path(self.work.name)
        slides = root / 'slides'
        slides.mkdir()
        (slides / '1.png').write_bytes(b'fixture')
        prompt = root / 'prompt.txt'
        prompt.write_text('严格讲稿格式，JSON 示例：{"pages": []}', encoding='utf-8')
        plan, _ = course.build_plan('原文依据。', {'duration_mode': mode, 'target_minutes': 1,
            'speech_rate': 200}, 4, 'zh')
        plan['slide_count'] = 1
        plan['sections'][0].update(first_page=1, last_page=1)
        self.calls = []
        def model(*args, **kwargs):
            self.calls.append(kwargs['prompt_text'])
            return outputs[min(len(self.calls) - 1, len(outputs) - 1)], {'tokens': 1}
        self.args = (plan, slides, prompt, {}, root, model)
        return course.generate_script(*self.args, workers=1)

    def test_overlong_repairs_once_and_valid_output_resumes_without_calls(self):
        text, _ = self.generate(['长' * 400 + '。 | no', '简' * 200 + '。 | no'])
        self.assertEqual(len(self.calls), 2)
        self.assertIn('180～220', self.calls[1])
        self.assertIn('{"pages": []}', self.calls[1])
        self.assertEqual(course.estimate_minutes(text, 'zh', 200), 1)
        course.generate_script(*self.args, workers=1)
        self.assertEqual(len(self.calls), 2)

    def test_budget_deviation_is_warning_and_resumes_without_infinite_retries(self):
        text, _ = self.generate(['长' * 400 + '。 | no'])
        self.assertEqual(len(self.calls), 2,'口播修订无变化时提前停止')
        from teaching_quality import script_report
        report = script_report(course.split_pages(text), self.args[0])
        self.assertTrue(report['passed'], report['issues'])
        self.assertEqual(report['issues'], [])
        self.assertTrue(any('时长仅供参考' in warning for warning in report['warnings']))
        self.assertFalse(course.read_json(self.args[4] / 'course_script/0.json')['needs_review'])
        course.generate_script(*self.args,workers=1)
        self.assertEqual(len(self.calls),2,'未达预算的已完成候选也不能在恢复时自动继续付费')

    def test_short_script_warns_without_content_gate(self):
        from teaching_quality import script_report
        report = script_report(['简短讲解。 | no'], {'speech_rate':200, 'script_lang':'zh', 'target_minutes':20})
        self.assertTrue(report['passed'])
        self.assertTrue(report['warnings'])

    def test_content_errors_still_block_when_duration_is_only_a_warning(self):
        from teaching_quality import script_report
        report = script_report(['2+2=5。 | no'], {'speech_rate':200, 'script_lang':'zh', 'target_minutes':20})
        self.assertFalse(report['passed'])
        self.assertTrue(report['issues'])
        self.assertTrue(report['warnings'])

    def test_auto_duration_and_valid_custom_duration_do_not_pay_for_repair(self):
        self.generate(['简' * 200 + '。 | no'])
        self.assertEqual(len(self.calls), 1)
        self.generate(['长' * 400 + '。 | no'], mode='auto')
        self.assertEqual(len(self.calls), 1)


if __name__ == '__main__':
    unittest.main()
