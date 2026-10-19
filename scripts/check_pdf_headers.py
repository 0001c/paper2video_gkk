"""Local PDF layout regressions. Optional: --source <chapter.pdf>; no model/server calls."""
import argparse
import json
import sys
import unittest
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import paper_ingest
from pdf_layout import analyze_pdf_layout

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'Paper2Video-main' / 'src'))
import course


def line(text, y, x=50, size=11, font='Body', bold=False):
    chars = [{'c': c, 'origin': (x + i * size * .5, y),
              'bbox': (x + i * size * .5, y - size, x + (i + 1) * size * .5, y + 1)}
             for i, c in enumerate(text)]
    return {'dir': (1, 0), 'spans': [{'chars': chars, 'text': text, 'size': size,
             'font': font, 'flags': 16 if bold else 0, 'origin': (x, y)}]}


class Page:
    rect = SimpleNamespace(width=500, height=700)
    rotation = 0

    def __init__(self, number, lines, fragmented=False):
        self.number, self.lines, self.fragmented = number, lines, fragmented

    def get_text(self, kind=None, **kwargs):
        lines = self.lines
        if self.fragmented:
            lines = [{'dir': ln['dir'], 'spans': [{**s, 'chars': [c], 'text': c['c'], 'origin': c['origin']}]}
                     for ln in lines for s in ln['spans'] for c in s['chars']]
        if kind in ('rawdict', 'dict'):
            return {'blocks': [{'type': 0, 'lines': [ln]} for ln in lines]}
        if kind == 'blocks':
            return [(0, 0, 0, 0, ''.join(s['text'] for s in ln['spans']), i, 0) for i, ln in enumerate(lines)]
        return '\n'.join(''.join(s['text'] for s in ln['spans']) for ln in lines)


class Document(list):
    @property
    def page_count(self):
        return len(self)

    def close(self):
        pass


def pages(headers=None, numbers=True, fragmented=False, math=False):
    headers = headers or ['2.1 First', '2.2 Second', '2.3 Third']
    result = Document()
    for i, text in enumerate(headers):
        rows = [line(text, 38, x=440 - len(text) * 5.5, font='Running')]
        rows += [line('2.%s Real section' % (i + 1), 72, size=13, font='Title', bold=True)]
        rows += [line('Body paragraph %s with content to retain.' % j, 110 + j * 20) for j in range(12)]
        if math:
            rows += [line('Equation α + β = 1 (2.3)', 400)]
        rows += [line('1 Footnote stays with the source.', 620, size=9)]
        if numbers:
            rows += [line(str(16 + i), 680, x=245, size=10)]
        result.append(Page(i, rows, fragmented))
    return result


def extract(document):
    with patch('pymupdf.open', return_value=document):
        return paper_ingest.extract_pdf(__file__)


class Headers(unittest.TestCase):
    def test_changing_headers_and_page_numbers(self):
        text, meta = extract(pages())
        self.assertEqual(sum(len(p['filtered_margins']) for p in meta['page_quality']), 6)
        self.assertNotIn('2.2 Second', text)
        self.assertIn('## 2.2 Real section', text)
        self.assertIn('Footnote stays', text)

    def test_first_changing_header_is_removed(self):
        self.assertEqual(analyze_pdf_layout(pages())[0]['removed'][1]['text'], '2.1 First')

    def test_two_page_same_header(self):
        result = analyze_pdf_layout(pages(['Same title', 'Same title'], numbers=False))
        self.assertEqual([len(p['removed']) for p in result], [1, 1])

    def test_two_page_changing_header_needs_corroboration(self):
        self.assertEqual([len(p['removed']) for p in analyze_pdf_layout(pages(['First', 'Second']))], [2, 2])
        self.assertFalse(any(p['removed'] for p in analyze_pdf_layout(pages(['First', 'Second'], numbers=False))))

    def test_single_page_uncertain_margins_stay(self):
        self.assertFalse(analyze_pdf_layout(pages(['Single title']))[0]['removed'])

    def test_no_header_body_lines_stay(self):
        document = pages(numbers=False)
        for p in document:
            p.lines = [line('Opening body text %s should stay.' % p.number, 55)] + p.lines[2:]
        self.assertFalse(any(p['removed'] for p in analyze_pdf_layout(document)))
        text, _ = extract(document)
        self.assertEqual(text.count('Opening body text'), 3)

    def test_body_gap_prevents_removing_repeated_first_sentence(self):
        document = pages(numbers=False)
        for p in document:
            p.lines = [line('Repeated first body sentence', 45)] + [
                line('More body content to retain on this page.', 65 + j * 20) for j in range(12)]
        self.assertFalse(any(p['removed'] for p in analyze_pdf_layout(document)))

    def test_repeated_body_line_is_not_enough(self):
        document = pages(numbers=False)
        for p in document:
            p.lines = [line('Repeated body text should stay.', 100)] + p.lines[2:]
        self.assertFalse(any(p['removed'] for p in analyze_pdf_layout(document)))

    def test_same_size_bold_top_sections_stay(self):
        document = pages(numbers=False)
        for p in document:
            p.lines[0] = line('2.%s Actual heading' % (p.number + 1), 38, size=11, font='Bold', bold=True)
        self.assertFalse(any(p['removed'] for p in analyze_pdf_layout(document)))

    def test_mirrored_running_headers(self):
        document = pages(['Odd title', 'Even title', 'Odd title', 'Even title'])
        for p in document[1::2]:
            p.lines[0] = line('Even title', 38, x=50, font='Running')
        self.assertEqual(sum(len(p['removed']) for p in analyze_pdf_layout(document)), 8)

    def test_nonsequential_numbers_and_footnotes_stay(self):
        document = pages()
        for p in document:
            p.lines[-1] = line(str(100 + p.number * 3), 680, x=245, size=10)
        result = analyze_pdf_layout(document)
        self.assertFalse(any(r['kind'] == 'page_number' for p in result for r in p['removed']))
        self.assertTrue(all('Footnote' not in r['text'] for p in result for r in p['removed']))

    def test_table_values_are_not_page_numbers(self):
        document = pages()
        for p in document:
            p.lines[-2] = line('Table values', 666, size=10)
        self.assertFalse(any(r['kind'] == 'page_number' for p in analyze_pdf_layout(document) for r in p['removed']))

    def test_rotated_pages_stay(self):
        document = pages()
        for p in document:
            p.rotation = 90
        self.assertFalse(any(p['removed'] for p in analyze_pdf_layout(document)))

    def test_two_columns_retain_all_body_content(self):
        document = pages(numbers=False)
        for p in document:
            p.lines = [line('Left column %s' % i, 80 + i * 20, x=45) for i in range(10)] + [
                line('Right column %s' % i, 80 + i * 20, x=270) for i in range(10)]
        text, meta = extract(document)
        self.assertEqual(text.count('Left column'), 30)
        self.assertEqual(text.count('Right column'), 30)
        self.assertFalse(any(p['filtered_margins'] for p in meta['page_quality']))

    def test_mixed_page_sizes_do_not_vote_together(self):
        document = pages(['First', 'Second'], numbers=False)
        document[1].rect = SimpleNamespace(width=600, height=800)
        self.assertFalse(any(p['removed'] for p in analyze_pdf_layout(document)))

    def test_fragmented_characters_use_same_filter(self):
        text, meta = extract(pages(fragmented=True))
        self.assertTrue(all(p['fragmented'] for p in meta['page_quality']))
        self.assertNotIn('2.2 Second', text)
        self.assertEqual(len(meta['layout_headings']), 3)

    def test_math_images_use_same_filter(self):
        text, meta = extract(pages(math=True))
        self.assertNotIn('2.2 Second', text)
        self.assertEqual(len(meta['glyph_images']), 3)
        self.assertEqual(len(paper_ingest.GLYPH_IMAGE_RE.findall(text)), 3)
        self.assertIn('Footnote stays', text)

    def test_missing_header_glyph_is_not_preserved_as_an_image(self):
        text, meta = extract(pages(['Unknown � title'] * 3))
        self.assertEqual(meta['glyph_images'], [])
        self.assertNotIn('Unknown', text)

    def test_numbered_bold_steps_do_not_become_chapters(self):
        document = pages()
        for p in document:
            p.lines[2] = line('1 Important numbered step', 110, font='Bold', bold=True)
        text, _ = extract(document)
        self.assertNotIn('# 1 Important', text)
        self.assertIn('1 Important numbered step', text)

    def test_pdf_sections_require_metadata_opt_in(self):
        text = '[源文第 1 页]\n\n## 2.1 True title\n\n2023.09 Chart label\n\n3.5 model continuation\n'
        original = course.source_sections(text)
        self.assertEqual(len(original), 4)
        self.assertEqual([s['title'] for s in course.source_sections(text, layout_headings=True)], ['全部材料', '2.1 True title'])
        self.assertEqual(course.source_sections(text), original)

    def test_long_heading_and_body_are_separate(self):
        document = pages()
        document[0].lines[1] = line('2.2.2 Chinchilla scaling laws with long title', 72, size=13, font='Title', bold=True)
        text, _ = extract(document)
        section = course.source_sections(text, layout_headings=True)[1]
        self.assertEqual(section['title'], '2.2.2 Chinchilla scaling laws with long title')


def real_source(source, out=None):
    text, meta = paper_ingest.extract_pdf(source)
    sections = course.source_sections(text, layout_headings=bool(meta['layout_headings']))
    titles = [s['title'] for s in sections[1:]]
    expected = ['第二章基础介绍', '2.1 大语言模型的构建过程', '2.1.1 大规模预训练',
                '2.1.2 指令微调与人类对齐', '2.2 扩展法则', '2.2.1 KM 扩展法则',
                '2.2.2 Chinchilla 扩展法则', '2.2.3 关于扩展法则的讨论', '2.3 涌现能力',
                '2.3.1 代表性的涌现能力', '2.3.2 涌现能力与扩展法则的关系',
                '2.4 GPT 系列模型的技术演变', '2.4.1 早期探索', '2.4.2 规模扩展',
                '2.4.3 能力增强', '2.4.4 性能跃升']
    assert titles == expected, titles
    margins = [r for p in meta['page_quality'] for r in p['filtered_margins']]
    assert Counter(r['kind'] for r in margins) == {'header': 16, 'page_number': 16}
    assert len(meta['glyph_images']) == len(paper_ingest.GLYPH_IMAGE_RE.findall(text)) == 23
    _, first = course.selected_source(text, [sections[2]['id']], layout_headings=True)
    assert '价值观对齐' in first and 'InstructGPT' in first and '## 2.2 扩展法则' not in first
    second_section = next(s for s in sections if s['title'] == '2.2 扩展法则')
    _, second = course.selected_source(text, [second_section['id']], layout_headings=True)
    assert all(title in second for title in expected[5:8]) and '## 2.3 涌现能力' not in second
    fourth = next(s for s in sections if s['title'] == '2.4 GPT 系列模型的技术演变')
    assert text.rfind('[源文第 ', 0, fourth['start']) == text.find('[源文第 12 页]')
    report = {'sections': sections, 'filtered_margins': margins, 'glyph_images': meta['glyph_images'],
              'verified': ['16 real headings', '16 headers + 16 page numbers', '23 glyph images',
                           'alignment stays in 2.1', 'complete 2.2', '2.4 begins on page 12']}
    if out:
        Path(out).mkdir(parents=True, exist_ok=True)
        Path(out, 'cleaned.txt').write_text(text, encoding='utf-8')
        Path(out, 'verification.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print('Real chapter PDF: all six content/layout checks passed')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--source')
    parser.add_argument('--out')
    args = parser.parse_args()
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(Headers))
    if not result.wasSuccessful():
        raise SystemExit(1)
    if args.source:
        real_source(args.source, args.out)
