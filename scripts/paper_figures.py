#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""论文配图提取：把 PDF / Word 里的插图导出成 LaTeX 工程可引用的文件。

## 为什么需要它（2026-09-19 实测）

上游流水线的入口是「**带图的** LaTeX 工程」（`--paper_latex_root`），
而我们的前端从 PDF/Word 出发，`paper_ingest` 只提取**正文文本** ——
于是 `latex_proj/` 里永远只有 `main.tex`，喂给幻灯片生成模型的文件清单就是
`['main.tex']`。中文提示词又明确写着「**若没有可用图片，不要写任何
`\\includegraphics`**」，于是幻灯片里 0 张图。

后果不只是"幻灯片不好看"：**树搜索（`--if_tree_search`）只会缩放
`\\includegraphics` 的宽度**，入选门槛是"Overfull 溢出的页且该 frame 含图片"
⇒ 在我们这条流水线里它**恒为空转**。实测两篇论文（其中一篇 PDF 内有 2 张
内嵌图）生成的 15 张幻灯片，`\\includegraphics` 全部是 0 次。

本模块补上这一环：从原始上传件里把图导出到 `<工程>/figures/`，
并写一份 `index.json` 供流水线生成"可用图片清单"塞进幻灯片提示词。

## 三条设计取舍

1. **只写 `figures/` 子目录，不改 `main.tex`。** 往 main.tex 里插 figure 环境
   会让 `STRUCT_FIELDS` 指纹失效（结构化缓存作废，白花一次钱），而图真正的
   消费方是**幻灯片生成**，不是正文 LaTeX。所以只提供文件 + 清单。
2. **图片格式一律转成 PNG/JPEG。** 期刊 PDF 里很常见 `.jpx`（JPEG2000），
   而 XeTeX/tectonic 的 `\\includegraphics` **不支持**它 —— 实测那篇有图的论文
   两张图都是 jpx。直接落原格式会得到一个"编译时才报错"的坑。
3. **文件名只用 ASCII**（`fig01_p06.png`）。本仓库已经被中文路径/中文编码坑过
   多次（LaTeX 宏包、stdout 编码、cv2.imread），示意图文件名不值得再赌一次；
   图的语义（标题、页号）写在 `index.json` 里，由提示词带给模型。

矢量图（matplotlib 之类画出来的）走 `page.cluster_drawings()` 兜底，但阈值
**未经标定**（手头两篇 PDF 各只聚类出 0 簇），所以规则保守、且标记 `kind=vector`
便于事后核对。
"""
import argparse
import hashlib
import io
import json
import os
import posixpath
import re
import shutil
import sys
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

# 改提取逻辑（阈值、命名、筛选）必须 +1 —— 它进 deck 指纹，
# 不 +1 就会命中"用旧逻辑提的图"生成的那批幻灯片，且不报错。
FIGURES_VERSION = "5"

FIGURES_DIRNAME = "figures"
INDEX_NAME = "index.json"
README_NAME = "README.txt"

# 工程里已存在这些扩展名的文件 ⇒ 认为"这个工程自带图"，不做提取
PROJECT_IMAGE_EXT = (".png", ".jpg", ".jpeg", ".pdf", ".eps", ".jbig2", ".jb2")

DEFAULT_MIN_SIDE = 150        # 源图短边下限（像素）：再小的基本是 logo/图标
DEFAULT_MIN_PAGE_RATIO = 0.03  # 显示区域占页面积下限：3%
DEFAULT_MAX_FIGURES = 40
DEFAULT_MAX_DIM = 2400        # 最长边超过就等比缩小（避免超大图拖慢编译）
DEFAULT_DPI = 200             # 矢量簇/裁剪区域按这个 dpi 渲染

VECTOR_MIN_PAGE_RATIO = 0.12  # 矢量簇：占页 ≥12%（未标定，保守）
VECTOR_MIN_WIDTH_RATIO = 0.30  # 且宽度 ≥ 页宽 30%
VECTOR_MIN_HEIGHT_RATIO = 0.10
VECTOR_MAX = 4                # 矢量图最多补这么多张

_CAPTION_LABEL = re.compile(r"^\s*(图|表|Figure|Fig\.?|Table|Tab\.?)\s*\d", re.I)


# ──────────────────────────────────────────────────────────────
# 小工具
# ──────────────────────────────────────────────────────────────
def _sha1_bytes(raw):
    return hashlib.sha1(raw).hexdigest()


def _clean_caption(text, limit=160):
    s = re.sub(r"\s+", " ", (text or "")).strip()
    s = s.strip("　 ")
    return s[:limit]


def figure_id(text):
    match = re.match(r'\s*(?:图|Fig(?:ure)?\.?)\s*(\d+(?:\s*[-–－]\s*\d+)?)', text, re.I)
    return 'F' + re.sub(r'\s+', '', match[1]).replace('–','-').replace('－','-') if match else None


def _text_rows(page):
    """图注碎成字符块时仍按字形坐标读完整行；不猜图题。"""
    chars = [char for block in page.get_text('rawdict')['blocks'] if block['type'] == 0
             for line in block['lines'] for span in line['spans'] for char in span['chars']]
    rows = []
    for char in sorted(chars, key=lambda c:(c['origin'][1],c['origin'][0])):
        if not rows or abs(char['origin'][1] - rows[-1][0]) > 0.75:
            rows.append((char['origin'][1], []))
        rows[-1][1].append(char)
    result = []
    for _, row in rows:
        row.sort(key=lambda c:c['origin'][0])
        kept = []
        for char in row:
            if not any(char['c'] == old['c'] and abs(char['origin'][0]-old['origin'][0]) < 0.8 for old in kept[-4:]):
                kept.append(char)
        row = kept
        result.append((min(c['bbox'][0] for c in row), min(c['bbox'][1] for c in row),
                       max(c['bbox'][2] for c in row), max(c['bbox'][3] for c in row), ''.join(c['c'] for c in row), 0, 0))
    return result


def _covered_figures(figures, limit):
    """先覆盖全章页域，再补同页图片；显式上限不能只留下前半章。"""
    if limit < 1:
        raise ValueError('max_figures 必须为正整数')
    if len(figures) <= limit:
        return figures
    pages = sorted({fig['page'] for fig in figures})
    chosen_pages = pages if len(pages) <= limit else [pages[round(i*(len(pages)-1)/max(1,limit-1))] for i in range(limit)]
    selected = [next(fig for fig in figures if fig['page'] == page) for page in chosen_pages]
    for fig in figures:
        if len(selected) >= limit:
            break
        if fig not in selected:
            selected.append(fig)
    return [fig for fig in figures if any(fig is selected_fig for selected_fig in selected)]


def _page_area(page):
    rect = page.rect
    return max(1.0, rect.width * rect.height)


def _overlap_ratio(a, b):
    """a、b 两个 bbox 的交集面积 / a 的面积。"""
    x0, y0 = max(a[0], b[0]), max(a[1], b[1])
    x1, y1 = min(a[2], b[2]), min(a[3], b[3])
    if x1 <= x0 or y1 <= y0:
        return 0.0
    inter = (x1 - x0) * (y1 - y0)
    area = max(1e-6, (a[2] - a[0]) * (a[3] - a[1]))
    return inter / area


def _h_overlap_ratio(x0, x1, bx0, bx1):
    """两条水平区间的重叠比例，分母取**较窄的那条**。

    ⚠️ 图题匹配只能用**水平**重叠（2026-09-19 踩过）：图题在图的正下方，
    两者的 y 区间**必然不相交**，用面积重叠算出来恒为 0 ⇒ 图题全部丢失。
    图题通常比图窄，所以分母取较窄者，这样"图题落在图的正下方"能得 1.0。
    """
    inter = min(x1, bx1) - max(x0, bx0)
    if inter <= 0:
        return 0.0
    return inter / max(1e-6, min(x1 - x0, bx1 - bx0))


def _caption_for(page, bbox, blocks):
    """在图片上下找图题。

    仅取以图/表编号开头的图题（`图 3 …` / `Figure 3 …`）。
    正文中的“如图9所示”不能作为图注，也不能抢占另一侧的真实图注。
    找不到返回空串 —— **不编造**。
    """
    below, above = [], []
    x0, y0, x1, y1 = bbox
    for b in blocks:
        if len(b) > 6 and b[6] != 0:       # 非文本块
            continue
        text = _clean_caption(b[4] if len(b) > 4 else "")
        if not text or not _CAPTION_LABEL.match(text):
            continue
        bx0, by0, bx1, by1 = b[0], b[1], b[2], b[3]
        h_overlap = _h_overlap_ratio(bx0, bx1, x0, x1)
        if y1 - 4 <= by0 <= y1 + 60 and h_overlap > 0.5:
            below.append((by0 - y1, bool(_CAPTION_LABEL.match(text)), text))
        elif y0 - 60 <= by1 <= y0 + 4 and h_overlap > 0.5:
            above.append((y0 - by1, bool(_CAPTION_LABEL.match(text)), text))
    for bucket in (below, above):
        if not bucket:
            continue
        labeled = [x for x in bucket if x[1]]
        pick = min(labeled, key=lambda x: x[0])
        return pick[2]
    return ""


def _save_png(raw, out_path, max_dim):
    """把任意可解码的图片字节写成 PNG（必要时等比缩小），返回 (宽, 高)。"""
    from PIL import Image
    with io.BytesIO(raw) as buf:
        with Image.open(buf) as im:
            im = im.convert("RGB") if im.mode not in ("RGB", "L") else im
            if max(im.size) > max_dim:
                ratio = max_dim / float(max(im.size))
                im = im.resize((max(1, int(im.size[0] * ratio)),
                                max(1, int(im.size[1] * ratio))), Image.LANCZOS)
            os.makedirs(os.path.dirname(out_path), exist_ok=True)
            im.save(out_path, "PNG", optimize=True)
            return im.size


def _save_jpeg(raw, out_path, max_dim):
    from PIL import Image
    with io.BytesIO(raw) as buf:
        with Image.open(buf) as im:
            im = im.convert("RGB")
            if max(im.size) > max_dim:
                ratio = max_dim / float(max(im.size))
                im = im.resize((max(1, int(im.size[0] * ratio)),
                                max(1, int(im.size[1] * ratio))), Image.LANCZOS)
            os.makedirs(os.path.dirname(out_path), exist_ok=True)
            im.save(out_path, "JPEG", quality=92, optimize=True)
            return im.size


def _pick_ext(fmt, want):
    """统一出口格式。返回 "jpg" 或 "png"。

    ⚠️ 为什么必须转：期刊 PDF 的内嵌图常见 `jpx`（JPEG2000），而 XeTeX/tectonic
    的 `\\includegraphics` **不支持** 它 —— 落原格式会变成"编译时才报错"。
    （实测那篇有图的论文，两张图都是 jpx。）

    为什么有损源转 JPEG 而不是 PNG：jpx 是有损压缩，转成 PNG 会把体积放大数倍
    （实测 161 KB 的 jpx → 1134 KB 的 PNG），而幻灯片里它只占约半页宽，
    JPEG q92 完全够用、体积回到 ~200 KB。无损源（png/bmp/tiff）仍留 PNG。
    """
    fmt = (fmt or "").lower()
    return "jpg" if fmt in ("jpeg", "jpg", "jpx", "jp2", "j2k") else "png"


# ──────────────────────────────────────────────────────────────
# PDF 提图
# ──────────────────────────────────────────────────────────────
def extract_from_pdf(pdf_path, figures_dir, min_side=DEFAULT_MIN_SIDE,
                     min_page_ratio=DEFAULT_MIN_PAGE_RATIO,
                     max_figures=DEFAULT_MAX_FIGURES, max_dim=DEFAULT_MAX_DIM,
                     dpi=DEFAULT_DPI, log=print):
    """提取 PDF 插图。返回 figure 字典列表（已去重、已排序）。

    ⚠️ 用 `doc.extract_image(xref)` 取**原始字节**（不是截图），
    这样分辨率最高；只有矢量簇才走 `get_pixmap(clip=...)`。
    """
    import pymupdf

    doc = pymupdf.open(pdf_path)
    seen_digest = {}
    figures = []
    try:
        for page_no, page in enumerate(doc, 1):
            area = _page_area(page)
            blocks = _text_rows(page)
            # ── 内嵌位图 ──
            image_infos = page.get_image_info(xrefs=True)
            for info in image_infos:
                bbox = tuple(info.get("bbox") or ())
                if len(bbox) != 4:
                    continue
                shown = (bbox[2] - bbox[0]) * (bbox[3] - bbox[1])
                if shown / area < min_page_ratio:
                    continue
                xref = info.get("xref")
                if not xref:
                    continue
                try:
                    meta = doc.extract_image(xref)
                except Exception as exc:
                    log("      [figures] 第 %d 页 xref=%s 取图失败：%s" % (
                        page_no, xref, type(exc).__name__))
                    continue
                raw = meta.get("image") or b""
                if not raw:
                    continue
                src_w = int(meta.get("width") or 0)
                src_h = int(meta.get("height") or 0)
                if min(src_w, src_h) < min_side:
                    continue
                digest = _sha1_bytes(raw)
                if digest in seen_digest:      # 同一张图重复出现：只留第一处
                    continue
                figures.append({
                    "page": page_no, "bbox": [round(v, 1) for v in bbox],
                    "src_width": src_w, "src_height": src_h,
                    "shown_ratio": round(shown / area, 4),
                    "kind": "raster", "format": (meta.get("ext") or "").lower(),
                    "caption": _caption_for(page, bbox, blocks),
                    "_raw": raw,
                })
                seen_digest[digest] = True

            # 带明确图号的碎片计算图可能由上千条1px位图组成，先合区域再筛尺寸。
            for caption in blocks:
                label = figure_id(caption[4])
                if (not _CAPTION_LABEL.match(caption[4]) or not label or len(caption[4]) > 60
                        or caption[2]-caption[0] > page.rect.width*0.75
                        or any(f['page'] == page_no and figure_id(f['caption']) == label for f in figures)):
                    continue
                floor = max([caption[1]-220, *[b[3]+2 for b in blocks if b[3] < caption[1]
                             and b[2]-b[0] > page.rect.width*0.6]])
                parts = [info['bbox'] for info in image_infos if floor <= info['bbox'][1] < info['bbox'][3] <= caption[1]+1]
                if not parts:
                    continue
                box = [min(b[0] for b in parts),min(b[1] for b in parts),max(b[2] for b in parts),max(b[3] for b in parts)]
                labels = [b for b in blocks if floor <= b[1] and b[3] < caption[1]
                          and len(re.findall(r'[\u3400-\u9fff]',b[4])) < 12
                          and _h_overlap_ratio(b[0],b[2],box[0],box[2]) > 0.5]
                if labels:
                    box[1] = min(box[1],min(b[1] for b in labels))
                    box[0] = min(box[0],min(b[0] for b in labels))
                    box[2] = max(box[2],max(b[2] for b in labels))
                pix = page.get_pixmap(clip=pymupdf.Rect(box),dpi=dpi)
                if min(pix.width,pix.height) < min_side/2:
                    continue
                figures.append({'page':page_no,'bbox':[round(v,1) for v in box],
                    'src_width':pix.width,'src_height':pix.height,'shown_ratio':round(pymupdf.Rect(box).get_area()/area,4),
                    'kind':'composite','format':'png','caption':_clean_caption(caption[4]),'_raw':pix.tobytes('png'),'merged_parts':len(parts)})

            # ── 矢量簇兜底（阈值未标定，保守）──
            raster_boxes = [f["bbox"] for f in figures if f["page"] == page_no]
            try:
                clusters = page.cluster_drawings()
            except Exception:
                clusters = []
            for rect in clusters:
                if len([f for f in figures if f["kind"] == "vector" and f['page'] == page_no]) >= VECTOR_MAX:
                    break
                w, h = rect.width, rect.height
                if w < VECTOR_MIN_WIDTH_RATIO * page.rect.width:
                    continue
                if h < VECTOR_MIN_HEIGHT_RATIO * page.rect.height:
                    continue
                if (w * h) / area < VECTOR_MIN_PAGE_RATIO:
                    continue
                box = [rect.x0, rect.y0, rect.x1, rect.y1]
                if any(_overlap_ratio(box, rb) > 0.5 for rb in raster_boxes):
                    continue
                try:
                    pix = page.get_pixmap(clip=rect, dpi=dpi)
                    raw = pix.tobytes("png")
                except Exception:
                    continue
                if not raw:
                    continue
                digest = _sha1_bytes(raw)
                if digest in seen_digest:
                    continue
                figures.append({
                    "page": page_no, "bbox": [round(v, 1) for v in box],
                    "src_width": pix.width, "src_height": pix.height,
                    "shown_ratio": round((w * h) / area, 4),
                    "kind": "vector", "format": "png",
                    "caption": _caption_for(page, box, blocks),
                    "_raw": raw,
                })
                seen_digest[digest] = True
        # 同一图号的相邻碎片合成一个源页裁剪，保留文字/矢量叠层。
        for fig in figures:
            fig['figure_id'] = figure_id(fig['caption'])
        grouped = []
        for fig in figures:
            parts = [part for part in figures if fig['figure_id'] and part['page'] == fig['page'] and part['figure_id'] == fig['figure_id']]
            if len(parts) > 1:
                if any(item.get('figure_id') == fig['figure_id'] and item['page'] == fig['page'] for item in grouped):
                    continue
                box = [min(p['bbox'][0] for p in parts), min(p['bbox'][1] for p in parts), max(p['bbox'][2] for p in parts), max(p['bbox'][3] for p in parts)]
                pix = doc[fig['page']-1].get_pixmap(clip=pymupdf.Rect(box),dpi=dpi)
                fig = {**fig,'bbox':box,'kind':'composite','format':'png','_raw':pix.tobytes('png'),
                       'src_width':pix.width,'src_height':pix.height,'merged_parts':len(parts)}
            grouped.append(fig)
        figures = grouped
    finally:
        doc.close()

    # 排序：先按页，同页内 raster 优先、再按显示面积从大到小
    figures.sort(key=lambda f: (f["page"], 0 if f["kind"] == "raster" else 1,
                                -f["shown_ratio"]))
    figures = _covered_figures(figures, max_figures)

    os.makedirs(figures_dir, exist_ok=True)
    out = []
    for i, fig in enumerate(figures, 1):
        ext = _pick_ext(fig.get("format"), fig["kind"])
        name = "fig%02d_p%02d.%s" % (i, fig["page"], ext)
        path = os.path.join(figures_dir, name)
        if ext == "jpg":
            w, h = _save_jpeg(fig["_raw"], path, max_dim)
        else:
            w, h = _save_png(fig["_raw"], path, max_dim)
        fig.pop("_raw", None)
        fig.update({"name": name, "rel": "%s/%s" % (FIGURES_DIRNAME, name),
                    "width": w, "height": h,
                    "bytes": os.path.getsize(path)})
        out.append(fig)
    return out


def _docx_figure_sources(zf):
    """按正文图片关系及紧邻图注绑定，不把媒体文件大小当成论文图号。"""
    namespaces = {'w': 'http://schemas.openxmlformats.org/wordprocessingml/2006/main',
                  'a': 'http://schemas.openxmlformats.org/drawingml/2006/main',
                  'r': 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'}
    relationships = {}
    for relation in ET.fromstring(zf.read('word/_rels/document.xml.rels')):
        if relation.get('TargetMode') == 'External':
            continue
        target = relation.get('Target', '')
        name = posixpath.normpath(target.lstrip('/') if target.startswith('/') else posixpath.join('word', target))
        if name.startswith('word/media/') and name in zf.namelist():
            relationships[relation.get('Id')] = name
    root = ET.fromstring(zf.read('word/document.xml'))
    rows = []
    for paragraph in root.findall('.//w:body//w:p', namespaces):
        text = ''.join(node.text or '' for node in paragraph.findall('.//w:t', namespaces))
        names = [relationships.get(node.get('{%s}embed' % namespaces['r']))
                 for node in paragraph.findall('.//a:blip', namespaces)]
        rows.append((text.strip(), [name for name in names if name]))
    label = re.compile(r'(?:图|Figure|Fig\.?)\s*\d+(?:\s*[-–－]\s*\d+)?', re.I)
    def captions(text):
        matches = list(label.finditer(text))
        if not matches or text[:matches[0].start()].strip():
            return []
        return [_clean_caption(text[match.start():matches[i+1].start() if i+1 < len(matches) else len(text)])
                for i, match in enumerate(matches)]
    numbered = any(captions(text) for text, _ in rows)
    result = []
    used_captions = set()
    for index, (text, names) in enumerate(rows):
        if not names:
            continue
        nearby = captions(text)
        caption_row = index
        if not nearby:
            # 忽略转换文档中的空白段落；遇到下一张图或正文即停止，避免跨段猜图注。
            for row_no, (following, images) in enumerate(rows[index+1:], index+1):
                if images:
                    break
                if following:
                    nearby = captions(following)
                    caption_row = row_no
                    break
        if not nearby:
            for row_no in range(index-1, -1, -1):
                preceding, images = rows[row_no]
                if images:
                    break
                if preceding:
                    nearby = captions(preceding)
                    caption_row = row_no
                    break
        if len(nearby) == len(names) and caption_row not in used_captions:
            result.extend(zip(names, nearby))
            used_captions.add(caption_row)
        elif not numbered:
            result.extend((name, '') for name in names)
    return result


def extract_from_docx(docx_path, figures_dir, min_side=DEFAULT_MIN_SIDE,
                      max_figures=DEFAULT_MAX_FIGURES, max_dim=DEFAULT_MAX_DIM,
                      log=print):
    """按正文图片顺序和原图注取图；有图注的论文跳过未绑定的装饰图。"""
    os.makedirs(figures_dir, exist_ok=True)
    picked = []
    try:
        with zipfile.ZipFile(docx_path) as zf:
            media = _docx_figure_sources(zf)
            seen = set()
            for name, caption in media:
                raw = zf.read(name)
                if not raw:
                    continue
                digest = _sha1_bytes(raw)
                identity = (digest, figure_id(caption))
                if identity in seen:
                    continue
                ext = os.path.splitext(name)[1].lower().lstrip(".")
                if ext not in ("png", "jpg", "jpeg", "bmp", "gif", "tif", "tiff", "emf", "wmf"):
                    continue
                try:
                    from PIL import Image
                    with io.BytesIO(raw) as buf:
                        with Image.open(buf) as im:
                            if min(im.size) < min_side:
                                continue
                            size = im.size
                except Exception:
                    continue
                seen.add(identity)
                picked.append((name, raw, ext, size, caption))
                if len(picked) >= max_figures:
                    break
    except Exception as exc:
        log("      [figures] 读取 docx 失败：%s: %s" % (type(exc).__name__, exc))
        return []

    out = []
    for i, (name, raw, ext, size, caption) in enumerate(picked, 1):
        use_jpg = ext in ("jpg", "jpeg")
        out_name = "fig%02d.%s" % (i, "jpg" if use_jpg else "png")
        path = os.path.join(figures_dir, out_name)
        if use_jpg:
            w, h = _save_jpeg(raw, path, max_dim)
        else:
            w, h = _save_png(raw, path, max_dim)
        out.append({"page": None, "bbox": None, "src_width": size[0], "src_height": size[1],
                    "shown_ratio": None, "kind": "docx", "format": ext,
                    "caption": caption, "figure_id": figure_id(caption), "docx_media": name, "name": out_name,
                    "rel": "%s/%s" % (FIGURES_DIRNAME, out_name),
                    "width": w, "height": h, "bytes": os.path.getsize(path)})
    return out


# ──────────────────────────────────────────────────────────────
# 工程级入口
# ──────────────────────────────────────────────────────────────
_GRAPHICS_RE = re.compile(r"\\includegraphics(?:\[[^\]]*\])?\s*\{([^}]+)\}")


def project_image_files(project_root):
    """工程里**被 `\\includegraphics` 真正引用**的图片文件（相对路径列表）。

    ⚠️ 为什么不是"目录里有没有图片文件"（2026-09-19 改）：
    LaTeX 工程里必然有 `main.pdf`（编译产物），按扩展名找会把它当成"自带图"
    ⇒ **纯文字论文**也被判成"已有图" ⇒ 提图被跳过、`figures` 指纹记成
    `own:n1:...` ⇒ 与"真的没有图"混为一谈，而且不报错。
    按 `\\includegraphics{...}` 引用找才是"这个工程真的会用的图"。

    这也正是上游的用法：使用者给的 LaTeX 工程里，图是被 main.tex 引用的。
    """
    found = set()
    if not os.path.isdir(project_root):
        return []
    for root, dirs, files in os.walk(project_root):
        dirs[:] = [d for d in dirs
                   if d not in ("__pycache__", ".git", FIGURES_DIRNAME)]
        for name in files:
            if not name.endswith(".tex"):
                continue
            try:
                text = io.open(os.path.join(root, name), encoding="utf-8",
                               errors="replace").read()
            except Exception:
                continue
            for m in _GRAPHICS_RE.finditer(text):
                raw = m.group(1).strip().strip('"\'')
                base = os.path.normpath(os.path.join(root, raw))
                for cand in (base, base + ".pdf", base + ".png", base + ".jpg",
                             base + ".jpeg", base + ".eps"):
                    if os.path.isfile(cand):
                        found.add(os.path.relpath(cand, project_root))
                        break
    return sorted(found)


def read_index(figures_dir):
    path = os.path.join(figures_dir, INDEX_NAME)
    if not os.path.isfile(path):
        return None
    try:
        with io.open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except Exception:
        return None


def _write_readme(figures_dir, data, log):
    lines = ["# 论文配图（由 scripts/paper_figures.py 提取）",
             "# 源：%s（%s）" % (data.get("source_name"), data.get("source_kind")),
             "# 版本：%s    共 %d 张" % (data.get("version"), len(data.get("figures") or [])),
             "#",
             "# 这张清单会由流水线附进幻灯片生成的提示词；本文件仅供人查看。", ""]
    for fig in data.get("figures") or []:
        cap = fig.get("caption") or "（未找到图题）"
        page = ("第 %s 页" % fig["page"]) if fig.get("page") else "—"
        lines.append("- %-22s %-8s %s  %sx%s  %s" % (
            fig["rel"], page, fig.get("kind"), fig.get("width"), fig.get("height"), cap))
    try:
        with io.open(os.path.join(figures_dir, README_NAME), "w",
                     encoding="utf-8", newline="") as handle:
            handle.write("\n".join(lines) + "\n")
    except Exception as exc:
        log("      [figures] README 写入失败：%s" % type(exc).__name__)


def _file_sha1(path):
    h = hashlib.sha1()
    with io.open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _discard_if_ours(figures_dir):
    """删掉本模块自己产生的文件；目录空了就连目录一起删。

    只碰 `index.json` / `README.txt` 与我们导出的 `fig_*` 图，**绝不递归删整个目录**
    —— 那个目录在"工程自带图"的情况下可能装着别人的东西（本仓库有过误删事故）。
    """
    if not os.path.isdir(figures_dir):
        return
    for name in (INDEX_NAME, README_NAME):
        p = os.path.join(figures_dir, name)
        if os.path.isfile(p):
            try:
                os.remove(p)
            except OSError:
                pass
    for name in os.listdir(figures_dir):
        if re.match(r"^fig\d+.*\.(png|jpg)$", name):
            p = os.path.join(figures_dir, name)
            if os.path.isfile(p):
                try:
                    os.remove(p)
                except OSError:
                    pass
    try:
        if not os.listdir(figures_dir):
            os.rmdir(figures_dir)
    except OSError:
        pass


def extract(source_path, project_root, *, force=False, log=print, **kwargs):
    """把 `source_path` 里的图提取到 `<project_root>/figures/`。

    幂等：`index.json` 存在、**且版本与源文件指纹都一致**才跳过（force=True 强制重做）。
    为什么要比源指纹：光比版本号的话，"工程目录被复用、但换了源文件"时会错误跳过，
    症状是"换了一篇论文，幻灯片里还是上一篇的图"——**不报错**。
    （同 `_upload_work` 的命中判据：判据必须包含输入本身。）

    **任何失败都不抛异常** —— 提图失败不该让整条流水线停下（图是增强项）。
    返回 (figures 列表, 说明字符串)。
    """
    figures_dir = os.path.join(project_root, FIGURES_DIRNAME)

    source_ok = bool(source_path) and os.path.isfile(source_path)
    source_digest = _file_sha1(source_path) if source_ok else None
    parameters = {key:kwargs.get(key,default) for key,default in (
        ('min_side',DEFAULT_MIN_SIDE),('min_page_ratio',DEFAULT_MIN_PAGE_RATIO),
        ('max_figures',DEFAULT_MAX_FIGURES),('max_dim',DEFAULT_MAX_DIM),('dpi',DEFAULT_DPI))}

    if not force:
        cached = read_index(figures_dir)
        if cached and str(cached.get("version")) == FIGURES_VERSION:
            if cached.get("source_sha1") and cached["source_sha1"] == source_digest and cached.get('parameters') == parameters:
                return (cached.get("figures") or [],
                        "已有 %d 张图的清单（版本 %s，源指纹一致），跳过提取" % (
                            len(cached.get("figures") or []), cached.get("version")))
            if not cached.get("source_sha1"):
                # 老清单没有源指纹（本版本之前写的）→ 保守重做
                log("      [figures] 已有清单缺少源指纹，重做一次以便可判定")

    # 工程自带图 ⇒ 不动它（那是使用者/上游给的资产，我们不做二次加工）
    existing = project_image_files(project_root)
    external = [p for p in existing if not p.startswith(FIGURES_DIRNAME + os.sep)]
    if external:
        return ([], "工程里已有 %d 个图片文件（例如 %s），不做提取" % (
            len(external), external[0]))

    if not source_ok:
        return ([], "源文件不可用：{}".format(source_path))

    ext = os.path.splitext(source_path)[1].lower()
    try:
        if ext == ".pdf":
            log("      [figures] 从 PDF 提取插图（短边≥%s px、占页≥%s）…" % (
                kwargs.get("min_side", DEFAULT_MIN_SIDE),
                kwargs.get("min_page_ratio", DEFAULT_MIN_PAGE_RATIO)))
            figures = extract_from_pdf(source_path, figures_dir, log=log, **kwargs)
            # These regions are required source content, not optional illustrations:
            # never discard them because of min_side/max_figures thresholds.
            from paper_ingest import extract_pdf, save_glyph_images
            from PIL import Image
            _, pdf_meta = extract_pdf(source_path)
            main = os.path.join(project_root, 'main.tex')
            code = Path(main).read_text(encoding='utf-8') if os.path.isfile(main) else ''
            glyphs = [image for image in pdf_meta['glyph_images'] if image['name'] in code]
            save_glyph_images(source_path, glyphs, figures_dir)
            for image in glyphs:
                with Image.open(os.path.join(figures_dir, image['name'])) as bitmap:
                    width, height = bitmap.size
                figures.append({**image, 'rel': 'figures/' + image['name'], 'figure_id': None,
                                'width': width, 'height': height, 'bytes': os.path.getsize(os.path.join(figures_dir, image['name']))})
            kind = "pdf"
        elif ext == ".docx":
            figures = extract_from_docx(source_path, figures_dir, log=log, **kwargs)
            kind = "docx"
        else:
            return ([], "该格式暂不提图（当前支持 .pdf / .docx）：%s" % ext)
    except Exception as exc:
        log("      [figures] ⚠️ 提图失败（{0}: {1}），本次幻灯片不会有图".format(
            type(exc).__name__, exc))
        return ([], "提图失败：{}".format(type(exc).__name__))

    data = {
        "version": FIGURES_VERSION,
        "source": os.path.abspath(source_path),
        "source_name": os.path.basename(source_path),
        "source_kind": kind,
        # 源指纹进清单：幂等跳过要以它为准（见 extract 的 docstring）
        "source_sha1": source_digest,
        'parameters':parameters,
        "figures": figures,
    }

    if not figures:
        # ⚠️ 没找到图就**什么都不留**（连 index.json 一起清掉）。
        # 为什么这一点很重要：工程目录会被整体复制给流水线，而提示词会收到
        # "工程里所有文件的路径清单"。留下 figures/index.json + README.txt 会让
        # **纯文字论文**的这份清单和改动前不再一致 ⇒ 它的 deck 指纹变化
        # ⇒ 使用者要为"其实什么都没变"再付一次幻灯片生成的钱。
        _discard_if_ours(figures_dir)
        return ([], "没找到符合条件的插图（可能是纯文字论文，或图太小/是装饰元素）")

    try:
        os.makedirs(figures_dir, exist_ok=True)
        with io.open(os.path.join(figures_dir, INDEX_NAME), "w",
                     encoding="utf-8", newline="") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
        _write_readme(figures_dir, data, log)
    except Exception as exc:
        log("      [figures] 清单写入失败：%s" % type(exc).__name__)

    return (figures, "提取 %d 张（%s）" % (len(figures), kind))


# ──────────────────────────────────────────────────────────────
# 给幻灯片生成模型的清单
# ──────────────────────────────────────────────────────────────
def figure_prompt_block(project_root, lang="zh", limit=None):
    """读 `<project_root>/figures/index.json`，拼一段"可用图片清单"给模型。

    为什么必须显式给清单：提示词只拿到**文件路径列表**（`figures/fig01_p06.png`
    这种 ASCII 名毫无语义），模型无从判断哪张图能用。清单把"文件名 → 图题/页码"
    对上，模型才能把图放到对的幻灯片上。
    """
    data = read_index(os.path.join(project_root, FIGURES_DIRNAME))
    if not data:
        return ""
    figures = data.get("figures") or []
    if not figures:
        return ""
    if limit:
        figures = figures[:limit]

    if lang == "zh":
        head = ("本项目已经提供了论文里的插图（放在 figures/ 下），"
                "请**优先**在内容匹配的幻灯片上用 \\includegraphics 引用它们："
                "宽度用相对值（如 width=0.8\\textwidth），给它加 caption，"
                "不要引用上面列表之外的图片。")
    else:
        head = ("The paper's own figures are provided under figures/. "
                "Please **prefer** \\includegraphics on the slides whose content "
                "matches them, with relative widths (e.g. width=0.8\\textwidth) and "
                "a caption. Do not reference any image not listed below.")
    head += ('\n每张源图必须在对应 figure 环境内注明清单给出的 figure_id（如 % source-figure: F2-13）与原图号，'
             'caption 必须完整逐字保留清单原图注；不得把神经网络图称为山谷/梯度图。无图号但有源页的图片须注明 % source-page: <source_page>；Word 图片无可靠页码时按清单路径引用，不得编造源页。'
             'kind=source_glyphs 是正文中无法转成文字的公式/字形原图：讲解对应内容时必须引用该图，不得省略、猜测或重新转写公式。'
             if lang == 'zh' else '\nBind every image within its figure environment using % source-figure: <figure_id> and its original caption. Never relabel a network diagram as a gradient valley.')
    lines = [head, ""]
    for fig in figures:
        cap = fig.get("caption") or ("page %s" % fig["page"] if fig.get("page") else "")
        lines.append("  - %s | figure_id=%s | source_page=%s | bbox=%s | %s" %
                     (fig['rel'],fig.get('figure_id') or 'unlabeled',fig.get('page'),fig.get('bbox'),cap) + ' | kind=' + fig.get('kind', ''))
    return "\n".join(lines)


# ──────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────
def main(argv=None):
    parser = argparse.ArgumentParser(description="从 PDF/Word 提取论文插图到 LaTeX 工程")
    parser.add_argument("--source", required=True, help="原始论文文件（.pdf/.docx）")
    parser.add_argument("--project", required=True, help="LaTeX 工程根目录（图写到它的 figures/ 下）")
    parser.add_argument("--force", action="store_true", help="忽略已有 index.json 重做")
    parser.add_argument("--min-side", type=int, default=DEFAULT_MIN_SIDE)
    parser.add_argument("--min-page-ratio", type=float, default=DEFAULT_MIN_PAGE_RATIO)
    parser.add_argument("--max", type=int, default=DEFAULT_MAX_FIGURES)
    parser.add_argument("--max-dim", type=int, default=DEFAULT_MAX_DIM)
    parser.add_argument("--dpi", type=int, default=DEFAULT_DPI)
    parser.add_argument("--lang", default="zh", help="清单语言（zh/en），用于打印预览")
    args = parser.parse_args(argv)

    figures, note = extract(
        args.source, args.project, force=args.force, log=print,
        min_side=args.min_side, min_page_ratio=args.min_page_ratio,
        max_figures=args.max, max_dim=args.max_dim, dpi=args.dpi)
    print("[figures] %s" % note)
    for fig in figures:
        print("   %-22s p%-4s %-7s %sx%s  %8.1f KB  %s" % (
            fig["rel"], fig.get("page") or "-", fig["kind"], fig["width"],
            fig["height"], fig["bytes"] / 1024, (fig.get("caption") or "")[:60]))
    block = figure_prompt_block(args.project, lang=args.lang)
    if block:
        print()
        print("=== 会附给幻灯片生成模型的清单（%s）===" % args.lang)
        print(block)
    return 0


if __name__ == "__main__":
    sys.exit(main())
