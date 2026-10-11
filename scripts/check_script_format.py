"""讲稿格式与定向重试离线回归，不调用真实模型。"""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'scripts'), str(ROOT / 'Paper2Video-main/src')]
import pv_env  # noqa: F401
import script_validation as scripts
import subtitle_cursor_prompt_gen as generator
import course
from PIL import Image


def structured(speech, cursor='no'):
    return json.dumps({'pages': [{'sentences': [{'speech_text': speech, 'cursor_prompt': cursor}]}]}, ensure_ascii=False)


class FormatChecks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=ROOT / 'jobs/_validation')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.image = self.root / '4.png'
        Image.new('RGB', (10, 10), 'white').save(self.image)
        self.prompt = self.root / 'prompt.txt'
        self.prompt.write_text('讲稿 | "原文短语"，每页用###分隔。', encoding='utf-8')

    def test_json_line_breaks_preserve_speech_numbers_and_cursor(self):
        text = structured('2016年，错误率15.3%。\n第二项为0.1。', '错误率\n15.3%')
        expected = [[['2016年，错误率15.3%。 第二项为0.1。', '错误率 15.3%']]]
        self.assertEqual(scripts.parse_script(text), expected)
        self.assertEqual(scripts.parse_script(scripts.normalize_script(text)), expected)
        # JSON字符串中没有转义的物理换行也只是排版；不补截断的引号或括号。
        self.assertEqual(scripts.parse_script(text.replace('\\n', '\n')), expected)

    def test_bare_no_only_repaired_as_cursor_value(self):
        text = structured('no是原文中的字词。').replace('"cursor_prompt": "no"', '"cursor_prompt": no')
        self.assertEqual(scripts.normalize_script(text), 'no是原文中的字词。 | no')
        malformed = structured('口播。').replace('"speech_text": "口播。"', '"speech_text": no')
        self.assertFalse(scripts.validate_script_output(malformed, 1)[0])

    def test_fenced_json_crlf_and_whitespace(self):
        text = '```JSON\r\n' + structured('解释内容。') + '\r\n```'
        self.assertEqual(scripts.normalize_script(text), '解释内容。 | no')

    def test_target_formula_pipe_is_not_spoken(self):
        for cursor in ('p(y|x)', '"p(y|x)"'):
            normalized = scripts.normalize_script(structured('解释条件概率。', cursor))
            self.assertEqual(scripts.parse_script(normalized), [[['解释条件概率。', '"p(y|x)"']]])
            self.assertEqual(course.spoken_text(normalized), '解释条件概率。')
        self.assertEqual(scripts.normalize_script('计算p(y|x)。 | "p(y|x)"'), '计算p(y|x)。 | "p(y|x)"')

    def test_multiple_quoted_targets_keep_all_phrases(self):
        text = structured('两个信息因素。', '"GCR" | "GCT"')
        self.assertEqual(scripts.normalize_script(text), '两个信息因素。 | "GCR" "GCT"')
        math = structured('解释。', '"p(y|x)" | "概率"')
        self.assertEqual(scripts.parse_script(scripts.normalize_script(math))[0][0][1], '"p(y|x)" "概率"')

    def test_no_guessing_truncated_or_ambiguous_fields(self):
        for text in (structured('内容。')[:-1], structured(''), structured('内容。', []),
                     structured('内容。', '说明 | "目标"'), structured('内容\x00。'),
                     '{"pages":[{"sentences":[]}]}', structured('页1。') + structured('页2。')):
            with self.subTest(text=text):
                self.assertFalse(scripts.validate_script_output(text, 1)[0])
        self.assertFalse(scripts.validate_script_output(structured('完整但只有一页。'), 2)[0])

    def test_format_retry_resets_context_and_reports_actual_reason(self):
        agent = MagicMock()
        invalid = '{"pages":['
        response = lambda text: SimpleNamespace(msg=SimpleNamespace(content=text), info={'usage': {'completion_tokens': 15}})
        agent.step.side_effect = [response(invalid), response(structured('有效内容。'))]
        with patch.object(generator, 'create_agent', return_value=agent), patch.object(generator.time, 'sleep'):
            result, _ = generator.subtitle_cursor_gen(str(self.root), str(self.prompt), {}, retries=2,
                image_paths=[str(self.image)], diagnostics_dir=self.root / 'responses')
        self.assertEqual(result, '有效内容。 | no')
        agent.reset.assert_called_once()
        retry_prompt = agent.step.call_args.args[0].content
        self.assertIn('覆盖上方旧的逐行讲稿格式', retry_prompt)
        self.assertIn('上次格式校验失败', retry_prompt)
        record = json.loads(next((self.root / 'responses').glob('*.json')).read_text(encoding='utf-8'))
        self.assertEqual(record['content'], invalid)
        self.assertEqual(record['images'], ['4.png'])
        self.assertEqual(record['usage'], {'completion_tokens': 15})

    def test_nonempty_format_error_has_no_balance_or_cutoff_guess(self):
        agent = MagicMock()
        agent.step.return_value = SimpleNamespace(msg=SimpleNamespace(content=structured('内容。')[:-1]), info={})
        with patch.object(generator, 'create_agent', return_value=agent), self.assertRaises(RuntimeError) as caught:
            generator.subtitle_cursor_gen(str(self.root), str(self.prompt), {}, retries=1, image_paths=[str(self.image)])
        self.assertIn('讲稿格式校验失败', str(caught.exception))
        self.assertNotIn('余额', str(caught.exception))
        self.assertNotIn('调大 PAPER2VIDEO_MAX_TOKENS', str(caught.exception))

    def test_cached_successful_pages_survive_format_failure(self):
        slides = self.root / 'slides'
        slides.mkdir()
        for number in (1, 2):
            Image.new('RGB', (10, 10), 'white').save(slides / f'{number}.png')
        calls = []
        def first(*args, **kwargs):
            number = Path(kwargs['image_paths'][0]).stem
            calls.append(number)
            if number == '2':
                raise ValueError('JSON格式失败')
            return '第一页正确讲稿。 | no', {}
        with self.assertRaisesRegex(RuntimeError, '第 2 页讲稿生成失败'):
            course.generate_script(None, slides, self.prompt, {}, self.root, first, workers=1)
        cached = (self.root / 'course_script/0.json').read_bytes()
        calls.clear()
        def retry(*args, **kwargs):
            calls.append(Path(kwargs['image_paths'][0]).stem)
            return structured('第二页恢复。'), {}
        result, _ = course.generate_script(None, slides, self.prompt, {}, self.root, retry, workers=1)
        self.assertEqual(calls, ['2'])
        self.assertEqual((self.root / 'course_script/0.json').read_bytes(), cached)
        self.assertTrue(scripts.validate_script_output(result, 2)[0])


if __name__ == '__main__':
    unittest.main(verbosity=2)
