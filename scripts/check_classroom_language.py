"""教学配置不进入课件和口播；真实知识中的身份、时间和光标原文不被误删。"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'scripts'), str(ROOT / 'Paper2Video-main/src')]
import artifact_cache
import course
import slide_quality
import teaching_quality
from classroom_language import CLASSROOM_PROMPT, classroom_issues


class ClassroomChecks(unittest.TestCase):
    def test_screenshot_labels_and_spoken_variants_are_detected(self):
        for text in (
            r'\textbf{研一学生专用} | 20 分钟核心讲解',
            '内容主要基于教材展开，旨在帮助研一学生快速掌握核心逻辑。',
            '在有限的二十分钟里，我们将聚焦最关键的知识点。',
            '这节课面向研究生。', '本课时长为20分钟。',
            'Graduate students only', '20-minute core lecture',
            '准确率达到50%，这节课面向研究生。',
        ):
            with self.subTest(text=text):
                self.assertTrue(classroom_issues(text))

    def test_subject_facts_and_natural_classroom_language_are_preserved(self):
        for text in (
            '同学们，这节课我们一起理解模型的构建过程。',
            '实验招募了20名研一学生，研究其学习行为。',
            '模型训练需要20分钟，每轮迭代约2秒。',
            '研究生参与问卷调查；20分钟后测量样本浓度。',
            'The study recruited graduate students and trained the model for 20 minutes.',
        ):
            with self.subTest(text=text):
                self.assertEqual(classroom_issues(text), [])

    def test_slide_gate_rejects_labels_without_rewriting_the_latex(self):
        code = r'\documentclass{beamer}\begin{frame}{课程}研一学生专用 | 20分钟核心讲解\end{frame}'
        self.assertTrue(any('教学配置泄漏' in issue for issue in slide_quality.quality_errors(code)))
        clean = code.replace('研一学生专用 | 20分钟核心讲解', '模型构建与扩展法则')
        self.assertEqual(slide_quality.quality_errors(clean), [])
        self.assertIn(CLASSROOM_PROMPT, slide_quality.LAYOUT_PROMPT)
        self.assertEqual(classroom_issues('% 研一学生专用\n同学们，这节课学习模型。', latex=True), [])

    def test_review_checks_narration_but_does_not_read_cursor_as_speech(self):
        report = teaching_quality.script_report(['研一学生专用，20分钟核心讲解。 | no'])
        self.assertFalse(report['passed'])
        self.assertTrue(any('教学配置泄漏' in issue for issue in report['issues']))
        good = '同学们，这节课我们学习模型构建。 | "研一学生专用 | 20分钟核心讲解"'
        self.assertTrue(teaching_quality.script_report([good])['passed'])

    def test_audience_and_timing_remain_available_as_internal_guidance(self):
        plan, _ = course.build_plan('大语言模型的构建过程。',
            {'audience': '研一学生', 'duration_mode': 'custom', 'target_minutes': 20}, 4, 'zh')
        prompt = course.slide_directive(plan)
        self.assertIn('受众背景（仅调节知识起点）：研一学生', prompt)
        self.assertIn('内部覆盖顺序与节奏', prompt)
        self.assertIn(CLASSROOM_PROMPT, prompt)
        self.assertEqual(plan['audience'], '研一学生')
        self.assertEqual(plan['target_minutes'], 20)

    def generation(self, responses):
        temporary = tempfile.TemporaryDirectory(dir=ROOT / 'jobs/_validation', prefix='classroom-')
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        slides = root / 'slide_imgs'
        slides.mkdir()
        (slides / '1.png').write_bytes(b'image-fixture')
        prompt = root / 'prompt.txt'
        prompt.write_text('输出讲稿', encoding='utf-8')
        calls = []

        def model(*args, **kwargs):
            calls.append(kwargs['prompt_text'])
            return responses[min(len(calls) - 1, len(responses) - 1)], {}

        policy = {'script_lang': 'zh', 'speech_rate': 220, 'examples': []}
        with patch.object(course, 'slide_topics', return_value=['模型构建']):
            text, _ = course.generate_script(None, slides, prompt, {}, root, model, teaching=policy)
            # 完成记录应复用，不因已标记待审核而无限付费重试。
            course.generate_script(None, slides, prompt, {}, root, model, teaching=policy)
        return root, text, calls

    def test_leaked_narration_is_revised_with_natural_classroom_language(self):
        root, text, calls = self.generation([
            '旨在帮助研一学生快速掌握核心逻辑。 | no',
            '同学们，这节课我们学习模型构建。 | no',
        ])
        self.assertEqual(len(calls), 2)
        self.assertIn(CLASSROOM_PROMPT, calls[0])
        self.assertIn('教学配置泄漏', calls[1])
        self.assertIn('同学们', text)
        self.assertTrue(course.read_json(root / 'teaching_report.json')['passed'])

    def test_persistent_leak_stops_and_remains_a_review_issue(self):
        root, _, calls = self.generation(['在有限的二十分钟里，我们学习模型构建。 | no'])
        self.assertEqual(len(calls), 2)
        self.assertTrue(course.read_json(root / 'course_script/0.json')['needs_review'])
        report = course.read_json(root / 'teaching_report.json')
        self.assertFalse(report['passed'])
        self.assertTrue(any('教学配置泄漏' in issue for issue in report['issues']))

    def test_old_deck_cache_keys_cannot_match_new_rules(self):
        facts = {'main_tex_sha1': 'source', 'script_generation_version': course.SCRIPT_GENERATION_VERSION}
        new = artifact_cache.deck_key(facts)
        with patch.object(artifact_cache, 'CACHE_VERSION', '5'):
            self.assertNotEqual(artifact_cache.deck_key(facts), new)


if __name__ == '__main__':
    unittest.main(verbosity=2)
