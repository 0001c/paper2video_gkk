"""编译失败诊断回归：保留退出码和原始输出，不调用模型或真实编译器。"""
import re
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'scripts'), str(ROOT / 'Paper2Video-main/src')]
import pv_env  # noqa: F401
import slide_code_gen_select_improvement as slides


class CompileDiagnosticsChecks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=ROOT / 'jobs/_validation')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.tex = self.root / 'slides.tex'
        self.code = (r'\documentclass{beamer}\begin{document}'
                     r'\begin{frame}content\end{frame}\end{document}')
        self.tex.write_text(self.code, encoding='utf-8')

    def compile(self, returncode=0, stdout='', stderr=''):
        result = SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)
        with patch.object(slides.subprocess, 'run', return_value=result):
            return slides.compile_tex(self.tex)

    def test_silent_failure_reports_native_exit_code_and_evidence_path(self):
        ok, feedback = self.compile(returncode=3221225477)
        self.assertFalse(ok)
        self.assertIn('3221225477 (0xC0000005)', feedback)
        self.assertIn(str(self.tex.with_suffix('.compile.raw.log')), feedback)
        self.assertFalse(re.search(r'^error: ', feedback, re.M))
        self.assertEqual(self.tex.with_suffix('.compile.log').read_text(encoding='utf-8'), feedback)
        self.assertIn('returncode: 3221225477 (0xC0000005)',
                      self.tex.with_suffix('.compile.raw.log').read_text(encoding='utf-8'))

    def test_noise_only_failure_keeps_raw_output_without_suggesting_model_repair(self):
        noise = ('Fontconfig error: Cannot load default config file\n'
                 'warning: accessing absolute path C:/Windows/Fonts/a.ttf')
        ok, feedback = self.compile(returncode=1, stderr=noise)
        self.assertFalse(ok)
        self.assertIn('退出码 1', feedback)
        self.assertNotIn('Fontconfig', feedback)
        self.assertNotIn('absolute path', feedback)
        self.assertFalse(re.search(r'^error: ', feedback, re.M))
        self.assertIn(noise, self.tex.with_suffix('.compile.raw.log').read_text(encoding='utf-8'))

    def test_quality_errors_are_saved_in_final_log(self):
        warning = r'warning: Overfull \vbox (40pt too high)'
        ok, feedback = self.compile(stderr=warning)
        self.assertFalse(ok)
        self.assertIn('error: 版面裁切', feedback)
        self.assertEqual(self.tex.with_suffix('.compile.log').read_text(encoding='utf-8'), feedback)
        raw = self.tex.with_suffix('.compile.raw.log').read_text(encoding='utf-8')
        self.assertIn('returncode: 0', raw)
        self.assertIn(warning, raw)
        self.assertNotIn('error: 版面裁切', raw)

    def test_normal_success_remains_successful(self):
        ok, feedback = self.compile(stdout='Output written on slides.pdf')
        self.assertTrue(ok)
        self.assertEqual(feedback, 'Output written on slides.pdf')
        self.assertEqual(self.tex.with_suffix('.compile.log').read_text(encoding='utf-8'), feedback)
        self.assertIn('returncode: 0', self.tex.with_suffix('.compile.raw.log').read_text(encoding='utf-8'))

    def test_real_compiler_error_is_retained(self):
        error = 'error: slides.tex: missing brace'
        ok, feedback = self.compile(returncode=1, stderr=error)
        self.assertFalse(ok)
        self.assertIn(error, feedback)
        self.assertIn('退出码 1', feedback)

    def test_silent_failure_stops_before_model_correction(self):
        prompt = self.root / 'prompt.txt'
        prompt.write_text('生成课件', encoding='utf-8')
        agent = MagicMock()
        agent.step.return_value = SimpleNamespace(
            msgs=[SimpleNamespace(content=self.code)], info={'usage': {}})
        result = SimpleNamespace(returncode=1, stdout='', stderr='')
        with patch.object(slides, 'create_agent', return_value=agent), \
                patch.object(slides.subprocess, 'run', return_value=result):
            with self.assertRaisesRegex(RuntimeError, '退出码 1'):
                slides.latex_code_gen_upgrade(prompt, self.root, self.tex, {}, {}, if_tree_search=False)
        self.assertEqual(agent.step.call_count, 1)


if __name__ == '__main__':
    unittest.main()
