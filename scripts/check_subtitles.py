"""Offline subtitle layout, configuration and HTTP/CLI regression checks."""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'Paper2Video-main/src'), str(ROOT / 'scripts')]

import subtitle_render as render
import subtitle_style
from web import app as web


class SubtitleChecks(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=ROOT / 'jobs/_validation')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def ass(self, text, style=None, size=42, width=2520, height=1418):
        target = self.root / 'sample.ass'
        render._write_ass([{'start': 12.25, 'end': 57.85, 'text': text}], target, width, height, size, 'Microsoft YaHei', style)
        return target.read_text(encoding='utf-8-sig')

    def test_default_and_partial_settings(self):
        self.assertEqual(subtitle_style.normalize(), subtitle_style.DEFAULTS)
        self.assertEqual(subtitle_style.normalize({'font_size': 48})['background_opacity'], 0.55)

    def test_font_must_be_a_name_instead_of_endpoint(self):
        for font in ('https://api.deepseek.com/v1', 'file:///fonts/Arial.ttf', '/fonts/Arial.ttf', 'C:/fonts/Arial.ttf'):
            with self.subTest(font=font), self.assertRaisesRegex(ValueError, 'subtitle_style.font_name'):
                subtitle_style.normalize({'font_name': font})
        for font in ('', '微软雅黑', 'Microsoft YaHei', 'Noto Sans CJK SC'):
            self.assertEqual(subtitle_style.normalize({'font_name': font})['font_name'], font)

    def test_invalid_values_and_ass_injection(self):
        invalid = [{'enabled': 'false'}, {'font_name': 'Arial,Injected'}, {'font_name': 'Font\nDialogue: 1'},
                   {'font_name': '{\\pos(0,0)}'}, {'text_color': 'red'}, {'background_color': '#000000FF'},
                   {'font_size': True}, {'font_size': 0}, {'font_size': 32.5}, {'background_opacity': float('nan')},
                   {'background_opacity': 1.1}, {'margin_horizontal': 50}, {'margin_bottom': -1}, {'line_spacing': 1.5}, {'line_spacing': float('inf')}, {'unknown': 1}, []]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValueError):
                subtitle_style.normalize(value)

    def test_chinese_long_paragraph_wrap_preserves_characters_and_clock(self):
        text = '自然语言处理连接人类语言与计算机。' * 13
        wrapped = render._wrap_text(text, 2268, 42)
        self.assertEqual(wrapped.replace('\n', ''), text)
        self.assertTrue(all(len(line) <= 45 for line in wrapped.splitlines()))
        with self.assertRaisesRegex(ValueError,'真实字词'):
            self.ass(text)

    def test_english_words_and_unbroken_tokens_wrap(self):
        text = 'Natural language processing ' * 8
        wrapped = render._wrap_text(text, 800, 40)
        self.assertEqual(' '.join(wrapped.split()), ' '.join(text.split()))
        self.assertTrue(all(len(line) <= 17 for line in wrapped.splitlines()))
        self.assertIn('\n', render._wrap_text('W' * 100, 800, 40))

    def test_explicit_newlines_and_ass_escape(self):
        content = self.ass('第一行\r\n第二行{\\pos(0,0)}')
        self.assertIn('第一行', content)
        self.assertIn(r'第二行\{\\pos(0,0)\}', content)
        events = [line for line in content.splitlines() if line.startswith('Dialogue:')]
        self.assertEqual(len(events), 2)
        self.assertTrue(all(line.startswith('Dialogue: 0,0:00:12.25,0:00:57.85') for line in events))

    def test_line_spacing_positions_preserve_timing_and_text(self):
        import re
        for spacing, difference in ((1.6, 67), (2.4, 101)):
            content = self.ass('第一行\n第二行', {'line_spacing': spacing})
            events = [line for line in content.splitlines() if line.startswith('Dialogue:')]
            positions = [int(re.search(r'\\pos\(1260,(\d+)\)', line)[1]) for line in events]
            self.assertEqual(positions[1] - positions[0], difference)
            self.assertEqual([line.split('}', 1)[1] for line in events], ['第一行', '第二行'])

    def test_bgr_colors_opacity_and_safe_margins(self):
        content = self.ass('样式测试', {'text_color': '#123456', 'background_color': '#ABCDEF',
            'background_opacity': 0.5, 'margin_horizontal': 10, 'margin_bottom': 5})
        self.assertIn('&H00563412', content)
        self.assertIn('&H80EFCDAB', content)
        self.assertIn(',2,252,252,71,1', content)

    def test_zero_opacity_disables_box(self):
        style = next(line for line in self.ass('透明', {'background_opacity': 0}).splitlines() if line.startswith('Style:'))
        self.assertEqual(style.split(',')[15], '1')

    def test_oversized_paragraph_without_timing_is_rejected(self):
        with self.assertRaisesRegex(ValueError,'真实字词'):
            self.ass('自然语言处理' * 90, size=96, width=1920, height=1080)

    def test_disabled_and_empty_captions_still_produce_output(self):
        source = self.root / 'source.mp4'
        source.write_bytes(b'no-paid-models')
        target = self.root / 'out.mp4'
        with patch.object(render, '_probe_resolution', side_effect=AssertionError('must not probe')):
            self.assertEqual(render.add_subtitles(source, target, style={'enabled': False}), target)
        self.assertEqual(target.read_bytes(), source.read_bytes())
        cursor = self.root / 'cursor.json'
        cursor.write_text('[]', encoding='utf-8')
        render.add_subtitles(source, target, cursor_json_path=cursor)
        self.assertEqual(target.read_bytes(), source.read_bytes())

    def test_http_normalization_and_cli_round_trip(self):
        config = {'slide_lang': 'zh', 'script_lang': 'zh', 'subtitle_style': {'font_size': 48, 'text_color': '#abcdef'}}
        web._normalize_models(config)
        argv = web._build_argv({'id': 'offline_subtitles', 'config': config, 'paper_latex_root': str(self.root)})
        decoded = json.loads(argv[argv.index('--subtitle-style') + 1])
        self.assertEqual(decoded, config['subtitle_style'])
        self.assertEqual(decoded['text_color'], '#ABCDEF')
        request = web.ReviewRunRequest(version='a' * 64, pages=['讲稿 | no'], action='render', subtitle_style=decoded)
        self.assertEqual(request.subtitle_style, decoded)

    def test_http_invalid_settings_report_field(self):
        with self.assertRaises(web.HTTPException) as caught:
            web._normalize_models({'subtitle_style': {'font_size': 1000}})
        self.assertEqual(caught.exception.status_code, 422)
        self.assertIn('subtitle_style.font_size', caught.exception.detail)


if __name__ == '__main__':
    unittest.main()
