"""Regression: incomplete PDF Unicode maps preserve source glyphs and cache assets.

Run: .venv/Scripts/python.exe scripts/smoke_pdf_fonts.py
No model calls or network requests.
"""
import hashlib
import json
import tempfile
import sys
from pathlib import Path
from unittest.mock import patch

import pymupdf
import artifact_cache
import paper_figures
import paper_ingest
import paper_struct


def main():
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root / 'Paper2Video-main' / 'src'))
    from slide_quality import quality_errors
    assert quality_errors('', 'warning: could not represent character "σ" in font "LatinModern"')
    assert not quality_errors('', 'warning: could not represent character "5" in font "nullfont"')
    assert not quality_errors(r'\begin{frame}\includegraphics{figures/docx.png}\end{frame}', figures=[{'rel': 'figures/docx.png', 'page': None, 'caption': ''}])
    validation = root / 'jobs' / '_validation'
    validation.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='pdf-font-smoke-', dir=validation) as directory:
        work = Path(directory)
        source = work / 'source.pdf'
        with pymupdf.open() as document:
            page = document.new_page()
            page.insert_text((72, 72), 'Before the formula')
            page.insert_text((72, 110), 'Equation A = 1')
            page.insert_text((72, 152), 'Following paragraph')
            font = page.get_fonts()[0][0]
            cmap = document.get_new_xref()
            document.update_object(cmap, '<<>>')
            chars = set('Before the formulaEquation A = 1Following paragraph')
            entries = '\n'.join('<%02x> <%04x>' % (ord(c), 0xfffd if c == 'A' else ord(c)) for c in chars)
            document.update_stream(cmap, ('/CIDInit /ProcSet findresource begin 12 dict begin begincmap '
                '/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def '
                '/CMapName /TestMap def /CMapType 2 def 1 begincodespacerange <00> <FF> endcodespacerange\n'
                '%d beginbfchar\n%s\nendbfchar endcmap CMapName currentdict /CMap defineresource pop end end'
                % (len(chars), entries)).encode())
            document.xref_set_key(font, 'ToUnicode', '%s 0 R' % cmap)
            document.save(source)

        text, meta = paper_ingest.ingest(str(source), str(work / 'ingest'))
        assert 'Before the formula' in text and 'Following paragraph' in text
        assert not meta['review_pages'] and not paper_ingest.has_unmapped_glyphs(text)
        assert len(meta['glyph_images']) == 1
        assert len(paper_ingest.GLYPH_IMAGE_RE.findall(text)) == 1
        image = meta['glyph_images'][0]
        with pymupdf.open(source) as document:
            expected = document[0].get_pixmap(clip=pymupdf.Rect(image['bbox']), dpi=300)
            actual = pymupdf.Pixmap(str(work / 'ingest' / 'glyph_images' / image['name']))
            assert expected.samples == actual.samples, 'Preserved glyphs must match the source pixels'

        with patch.object(paper_struct, 'call_llm', side_effect=AssertionError('PDF must stay local')), \
                patch.object(paper_struct, 'compile_tex', return_value=(True, '')):
            tex, _ = paper_struct.structurize(str(source), str(work / 'structured'))
        project = work / 'structured' / 'latex_proj'
        assert image['name'] in tex and '[原文字形图：' not in tex
        assert paper_struct.verified_source_binding(project / 'main.tex', str(source))
        figures, _ = paper_figures.extract(str(source), str(project), max_figures=1, min_side=9999)
        assert any(fig['name'] == image['name'] and fig['kind'] == 'source_glyphs' for fig in figures)
        assert 'source_glyphs' in paper_figures.figure_prompt_block(str(project))
        with patch.object(artifact_cache, 'CACHE_ROOT', str(work / 'cache')):
            facts = {'v': artifact_cache.CACHE_VERSION, 'source_sha1': hashlib.sha1(source.read_bytes()).hexdigest()}
            cache = Path(artifact_cache.publish_struct(facts, str(project / 'main.tex')))
            assert paper_struct.verified_source_binding(cache / 'main.tex', str(source))
            binding = json.loads((cache / 'source_binding.json').read_text(encoding='utf-8'))
            assert binding['glyph_images']
            cached_image = cache / next(iter(binding['glyph_images']))
            cached_image.write_bytes(b'corrupted')
            assert not paper_struct.verified_source_binding(cache / 'main.tex', str(source))

        scanned = work / 'scanned.pdf'
        with pymupdf.open() as document:
            document.new_page()
            document.save(scanned)
        assert paper_ingest.extract_pdf(str(scanned))[1]['review_pages'] == [1]
    print('PDF font regression passed: source pixels, local layout, required images, cache integrity, scanned-page guard')


if __name__ == '__main__':
    main()
