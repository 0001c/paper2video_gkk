"""验证 PDF 软蒙版源图不会变成黑块或错误去重。"""
import io
import sys
import tempfile
import unittest
from pathlib import Path

import pymupdf
from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import paper_figures


class FigureMaskChecks(unittest.TestCase):
    def test_same_black_base_with_distinct_masks_preserves_visible_content(self):
        with tempfile.TemporaryDirectory(dir=ROOT / 'jobs/_validation', prefix='figure-masks-') as work:
            root = Path(work)
            source = root / 'source.pdf'
            document = pymupdf.open()
            for index in range(2):
                bitmap = Image.new('RGBA', (300, 200), (0, 0, 0, 0))
                ImageDraw.Draw(bitmap).rectangle((20 + index * 80, 20, 60 + index * 80, 160), fill=(0, 0, 0, 255))
                data = io.BytesIO()
                bitmap.save(data, format='PNG')
                page = document.new_page(width=400, height=300)
                page.insert_image(pymupdf.Rect(50, 50, 350, 250), stream=data.getvalue())
            document.save(source)
            document.close()
            output = root / 'figures'
            figures = paper_figures.extract_from_pdf(str(source), str(output), min_side=100, log=lambda _: None)
            self.assertEqual(len(figures), 2, '不同软蒙版不能按同一黑色底图去重')
            for figure in figures:
                image = Image.open(output / Path(figure['rel']).name).convert('RGB')
                self.assertEqual(image.getpixel((0, 0)), (255, 255, 255))
                self.assertGreater(sum(image.resize((1, 1)).getpixel((0, 0))), 500)


if __name__ == '__main__':
    unittest.main()
