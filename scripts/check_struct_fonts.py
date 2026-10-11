"""结构化字体及缓存回归：不调用模型，实际编译保留全部 Unicode 原文。"""
import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'Paper2Video-main/src'))
import pv_env  # noqa: F401
import artifact_cache
import paper_struct


class StructFontChecks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=ROOT / 'jobs/_validation')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def bound_project(self, compile_ok=True):
        tex = self.root / 'project/main.tex'
        tex.parent.mkdir()
        tex.write_text('original source', encoding='utf-8')
        assets = {}
        for name in paper_struct.FONT_ASSET_NAMES:
            target = tex.parent / name
            target.parent.mkdir(exist_ok=True)
            target.write_bytes(b'font fixture')
            assets[name] = hashlib.sha256(target.read_bytes()).hexdigest()
        binding = {'version': 1, 'compile_ok': compile_ok,
                   'tex_sha256': hashlib.sha256(tex.read_bytes()).hexdigest(), 'font_assets': assets}
        tex.with_name('source_binding.json').write_text(json.dumps(binding), encoding='utf-8')
        return tex

    def test_font_assets_survive_cache_and_detect_damage(self):
        tex = self.bound_project()
        facts = {'source_sha1': 'font-source'}
        with patch.object(artifact_cache, 'CACHE_ROOT', str(self.root / 'cache')):
            self.assertIsNotNone(artifact_cache.publish_struct(facts, str(tex)))
            cached, _, _ = artifact_cache.lookup_struct(facts)
            self.assertTrue(paper_struct.verified_source_binding(cached))
            (Path(cached).parent / paper_struct.FONT_ASSET_NAMES[0]).write_bytes(b'damaged')
            self.assertFalse(paper_struct.verified_source_binding(cached))
            self.assertIsNone(artifact_cache.lookup_struct(facts)[0])

    def test_failed_compile_is_neither_published_nor_reused(self):
        tex = self.bound_project(False)
        facts = {'source_sha1': 'failed-source'}
        with patch.object(artifact_cache, 'CACHE_ROOT', str(self.root / 'cache')):
            self.assertIsNone(artifact_cache.publish_struct(facts, str(tex)))
            # 模拟修复前已经保存的失败缓存，保留文件但拒绝命中。
            directory = Path(artifact_cache.cache_dir('struct', artifact_cache.struct_key(facts)))
            directory.mkdir(parents=True)
            (directory / 'main.tex').write_bytes(tex.read_bytes())
            (directory / 'source_binding.json').write_bytes(tex.with_name('source_binding.json').read_bytes())
            self.assertIsNone(artifact_cache.lookup_struct(facts)[0])

    def test_font_path_cannot_escape_project(self):
        tex = self.bound_project()
        binding_path = tex.with_name('source_binding.json')
        binding = json.loads(binding_path.read_text(encoding='utf-8'))
        binding['font_assets'] = {'../outside.ttf': 'hash'}
        binding_path.write_text(json.dumps(binding), encoding='utf-8')
        self.assertFalse(paper_struct.verified_source_binding(tex))

    def test_missing_glyph_remains_failure_and_nullfont_noise_is_allowed(self):
        for font, expected in [('LatinModern', False), ('nullfont', True)]:
            result = SimpleNamespace(returncode=0, stdout='', stderr=(
                'warning: could not represent character "α" in font "{}"'.format(font)))
            with patch.object(paper_struct.subprocess, 'run', return_value=result):
                self.assertEqual(paper_struct.compile_tex(str(self.root / 'main.tex'))[0], expected)

    def test_no_native_fonts_keeps_existing_portable_preamble(self):
        with patch.dict(os.environ, {'WINDIR': str(self.root / 'no-windows')}):
            tex, assets = paper_struct.prepare_source_fonts('original', self.root)
        self.assertEqual((tex, assets), ('original', {}))

    @unittest.skipUnless((Path(os.environ.get('WINDIR', 'C:/Windows')) / 'Fonts/cambria.ttc').is_file(),
                         '本机未提供 Windows 字体')
    def test_real_compilation_preserves_math_symbols_and_author_names(self):
        text = '郭海玲与玥，αχδη∈●◯○⊗△，Božić与Mă，p=0.05。'
        source = self.root / 'source.txt'
        source.write_text(text, encoding='utf-8')
        with patch.object(paper_struct, 'call_llm', side_effect=AssertionError('不得调用模型')):
            tex, report = paper_struct.structurize(str(source), str(self.root / 'output'), text_file=source)
        self.assertTrue(report['compile_ok'])
        self.assertIn(text, tex)
        self.assertTrue(paper_struct.verified_source_binding(report['tex_path'], source, source))
        self.assertNotIn('could not represent character', (self.root / 'output/struct_compile.log').read_text(encoding='utf-8'))


if __name__ == '__main__':
    unittest.main(verbosity=2)
