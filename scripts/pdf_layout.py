"""Conservative PDF margin and heading evidence, before text loses its layout."""
import re
from collections import Counter
from statistics import median


_TITLE = re.compile(r'^(?:第[一二三四五六七八九十百\d]+[章节]|\d+(?:\.\d+){0,4}\s+\S)')


def in_regions(origin, regions):
    return any(box['bbox'][0] - 1 <= origin[0] <= box['bbox'][2] + 1
               and box['bbox'][1] - 1 <= origin[1] <= box['bbox'][3] + 1 for box in regions)


def _rows(page):
    """Join fragmented drawing runs only for layout analysis; keep normal reading order elsewhere."""
    chars = []
    for block in page.get_text('rawdict')['blocks']:
        if block['type'] != 0:
            continue
        for line in block['lines']:
            if abs(line['dir'][0] - 1) > .01 or abs(line['dir'][1]) > .01:
                continue
            for span in line['spans']:
                chars.extend({**char, 'size': span['size'], 'font': span['font'], 'flags': span['flags']}
                             for char in span['chars'])
    bands = []
    for char in sorted(chars, key=lambda c: (c['origin'][1], c['origin'][0])):
        if not bands or abs(char['origin'][1] - bands[-1][0]) > .75:
            bands.append((char['origin'][1], []))
        bands[-1][1].append(char)
    rows = []
    for _, band in bands:
        kept = []
        for char in sorted(band, key=lambda c: c['origin'][0]):
            if not any(char['c'] == old['c'] and abs(char['origin'][0] - old['origin'][0]) < .8
                       for old in kept[-4:]):
                kept.append(char)
        text = ''.join(c['c'] for c in kept).strip()
        if not text:
            continue
        sizes = Counter(round(c['size'], 1) for c in kept if c['c'].strip())
        fonts = Counter(c['font'] for c in kept if c['c'].strip())
        rows.append({'text': text,
                     'bbox': [min(c['bbox'][0] for c in kept), min(c['bbox'][1] for c in kept),
                              max(c['bbox'][2] for c in kept), max(c['bbox'][3] for c in kept)],
                     'size': sizes.most_common(1)[0][0], 'font': fonts.most_common(1)[0][0],
                     'bold': sum(bool(c['flags'] & 16) for c in kept) / len(kept)})
    return rows


def _page_layout(page):
    # Rotated text needs a different coordinate/reading-order model. Leave it intact.
    rows = _rows(page) if not page.rotation else []
    width, height = page.rect.width, page.rect.height
    body = [r for r in rows if height * .12 <= r['bbox'][1] and r['bbox'][3] <= height * .88]
    sizes, fonts = Counter(), Counter()
    for row in body:
        sizes[row['size']] += len(row['text'])
        fonts[row['font']] += len(row['text'])
    gaps = [b['bbox'][1] - a['bbox'][3] for a, b in zip(body, body[1:])
            if b['bbox'][1] > a['bbox'][3] and abs(a['size'] - b['size']) < .6]
    return {'page': page.number + 1, 'width': width, 'height': height, 'rows': rows,
            'body_size': sizes.most_common(1)[0][0] if sizes else 0,
            'body_font': fonts.most_common(1)[0][0] if fonts else '',
            'body_gap': median(gaps) if gaps else 0}


def _page_number(text):
    match = re.fullmatch(r'\s*(?:[-–—]\s*)?(?:第\s*)?(\d{1,4})(?:\s*页)?(?:\s*[-–—])?\s*', text)
    return int(match[1]) if match else None


def _same_slot(a, b):
    if (abs(a['width'] - b['width']) > 2 or abs(a['height'] - b['height']) > 2
            or a['font'] != b['font'] or abs(a['size'] - b['size']) > .6
            or abs(a['bbox'][1] - b['bbox'][1]) > 2):
        return False
    # Accommodate centred numbers and both left/right aligned running titles.
    return min(abs(a['bbox'][0] - b['bbox'][0]), abs(a['bbox'][2] - b['bbox'][2]),
               abs(sum(a['bbox'][::2]) - sum(b['bbox'][::2])) / 2) <= max(3, a['width'] * .008)


def analyze_pdf_layout(document):
    """Two passes: require repeated edge *layout*, never crop a page or deduplicate titles."""
    pages = [_page_layout(page) for page in document]
    sizes = Counter(p['body_size'] for p in pages if p['body_size'])
    document_size = sizes.most_common(1)[0][0] if sizes else 0
    for page in pages:
        # A full-page chart's tiny labels are not the document's body font.
        page['body_size'] = max(page['body_size'], document_size)
    groups = {'header': [], 'page_number': []}
    for page in pages:
        rows = page['rows']
        if len(rows) < 3 or not page['body_size']:
            continue
        for kind, row, neighbour in [('header', rows[0], rows[1]), ('page_number', rows[-1], rows[-2])]:
            top = kind == 'header'
            gap = neighbour['bbox'][1] - row['bbox'][3] if top else row['bbox'][1] - neighbour['bbox'][3]
            if (gap < max(8, row['size'] * .8, page['body_gap'] * 1.4) or len(row['text']) > 120
                    or (top and row['bbox'][3] > page['height'] * .12)
                    or (not top and row['bbox'][1] < page['height'] * .88)):
                continue
            # Same-sized bold numbered headings can recur at the top of real sections.
            if top and (row['size'] > page['body_size'] * 1.10
                        or (_TITLE.match(row['text']) and row['bold'] >= .5)):
                continue
            if not top and _page_number(row['text']) is None:
                continue  # Footnotes/captions are content, even when repeated.
            candidate = {**row, **{k: page[k] for k in ('page', 'width', 'height', 'body_size', 'body_font')}}
            group = next((g for g in groups[kind] if _same_slot(candidate, g[0])), None)
            if group is None:
                groups[kind].append([candidate])
            else:
                group.append(candidate)

    removed = {p['page']: [] for p in pages}
    numbered_pages = set()
    for group in groups['page_number']:
        offsets = Counter(_page_number(r['text']) - r['page'] for r in group)
        for row in group:
            if offsets[_page_number(row['text']) - row['page']] >= 2:
                numbered_pages.add(row['page'])
                removed[row['page']].append({**row, 'kind': 'page_number', 'reason': 'isolated_edge_sequential_page_numbers'})
    for group in groups['header']:
        if len(group) < 2:
            continue
        texts = Counter(re.sub(r'\s+', '', r['text']) for r in group)
        for row in group:
            exact = texts[re.sub(r'\s+', '', row['text'])] >= 2
            # A changing first body line also repeats its baseline/alignment.
            # Require distinct margin typography and a position above the body
            # before generalising a running-title slot to unseen section names.
            changing = (row['font'] != row['body_font']
                        and row['bbox'][3] <= row['height'] * .07
                        and row['bbox'][2] - row['bbox'][0] < row['width'] * .85
                        and (len(group) >= 3 or (row['page'] in numbered_pages
                             and sum(r['page'] in numbered_pages for r in group) >= 2)))
            if exact or changing:
                removed[row['page']].append({**row, 'kind': 'header', 'reason': 'repeated_isolated_edge_layout'})

    result = []
    for page in pages:
        excluded = removed[page['page']]
        headings = []
        for row in page['rows']:
            if (not _TITLE.match(row['text']) or len(row['text']) > 120
                    or re.search(r'(?:\.\s*){4,}|…{2,}', row['text'])
                    or in_regions(((row['bbox'][0] + row['bbox'][2]) / 2,
                                   (row['bbox'][1] + row['bbox'][3]) / 2), excluded)):
                continue
            if page['body_size'] and (row['size'] >= page['body_size'] * 1.08
                                     or (row['bold'] >= .6 and row['size'] >= page['body_size'] * .95)):
                number = re.match(r'\d+(?:\.\d+)*', row['text'])
                if number and '.' not in number[0] and row['size'] < page['body_size'] * 1.2:
                    continue  # Same-sized bold numbered steps are not chapter evidence.
                level = number[0].count('.') + 1 if number else 1
                headings.append({**row, 'level': min(level, 6)})
        result.append({'removed': [{k: r[k] for k in ('text', 'bbox', 'kind', 'reason')} for r in excluded],
                       'headings': headings})
    return result
