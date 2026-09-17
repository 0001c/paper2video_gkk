#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""gkk 三组合产出的客观验证。

跑完三个组合后执行：
    python scripts/verify_gkk_3combos.py

只做「能量的」——观感类判断（语速听感、口型自然度、版式美观）留给用户。
覆盖五类可以客观判定的事：

1. 成片元数据：时长 / 分辨率 / 帧率 / **是否有音轨**（无音轨是最常见的静默失败）
2. 讲稿语言：页数、句数、汉字占比、光标目标是否为「引用幻灯片原文」
3. 光标定位：PDF 命中率、回退 grounding 条数、**落点是否落在所引用短语的矩形内**、
   同页落点有无重复（重复会造成"指着一个地方不动"）
4. 数字人：页数与音频数是否一致、逐页时长是否等于对应音频时长
5. 内容证据：数字人独立小视频里「嘴部带 vs 眼部带」的帧间变化比
   （口型在动、头部静止的特征；对新换的人像尤其要验）
"""
import collections
import json
import os
import re
import subprocess
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SRC = os.path.join(ROOT, 'Paper2Video-main', 'src')
sys.path.insert(0, SRC)

COMBOS = ['gkk_en', 'gkk_en_zh', 'gkk_zh']
LABEL = {
    'gkk_en': '英slide + 英讲稿',
    'gkk_en_zh': '英slide + 中讲稿',
    'gkk_zh': '中slide + 中讲稿',
}

FFPROBE = None
for cand in ('ffprobe', 'ffprobe.exe'):
    from shutil import which
    FFPROBE = which(cand)
    if FFPROBE:
        break


def hr(title):
    print()
    print('=' * 70)
    print('  ' + title)
    print('=' * 70)


def probe(path):
    out = subprocess.run(
        [FFPROBE, '-v', 'error', '-show_format', '-show_streams',
         '-print_format', 'json', path],
        capture_output=True, text=True, encoding='utf-8', errors='replace').stdout
    return json.loads(out)


def wav_dur(p):
    import contextlib
    import wave
    with contextlib.closing(wave.open(p)) as w:
        return w.getnframes() / w.getframerate()


def check_merge(base, name):
    hr('%s  ① 成片元数据' % name)
    p = os.path.join(base, '3_merage.mp4')
    if not os.path.exists(p):
        print('   ⚠️ 3_merage.mp4 不存在'); return
    j = probe(p)
    v = [s for s in j['streams'] if s['codec_type'] == 'video'][0]
    a = [s for s in j['streams'] if s['codec_type'] == 'audio']
    dur = float(j['format']['duration'])
    print('   文件      : %.2f MB' % (os.path.getsize(p) / 2**20))
    print('   时长      : %.2f s  (%.2f min)' % (dur, dur / 60))
    print('   视频      : %s %sx%s %s fps' % (
        v['codec_name'], v['width'], v['height'], v.get('r_frame_rate')))
    print('   音轨      : %s' % (
        '%s %s Hz %s ch' % (a[0]['codec_name'], a[0].get('sample_rate'),
                            a[0].get('channels')) if a else '⚠️ 无音轨！'))
    print('   中间产物  : 1_merage=%s  2_merage=%s' % (
        '有' if os.path.exists(os.path.join(base, '1_merage.mp4')) else '无',
        '有' if os.path.exists(os.path.join(base, '2_merage.mp4')) else '无'))


def check_script(base, name):
    hr('%s  ② 讲稿语言与光标目标' % name)
    p = os.path.join(base, 'subtitle_w_cursor.txt')
    if not os.path.exists(p):
        print('   ⚠️ 讲稿不存在'); return None
    txt = open(p, encoding='utf-8').read()
    pages = [x for x in txt.strip().split('###') if x.strip()]
    lines = [l for l in txt.splitlines() if '|' in l]
    cjk_all = len(re.findall(r'[\u4e00-\u9fff]', txt))
    ratio = 100.0 * cjk_all / max(len(re.sub(r'\s', '', txt)), 1)
    quoted = sum(1 for l in lines if '"' in l.split('|', 1)[1])
    print('   页数      : %d' % len(pages))
    print('   句数      : %d' % len(lines))
    print('   汉字占比  : %.0f%%  （>40%% 视为中文讲稿）' % ratio)
    print('   光标目标带引号（引用原文）: %d / %d' % (quoted, len(lines)))
    print()
    print('   --- 首句样例 ---')
    for l in lines[:2]:
        left, right = l.split('|', 1)
        print('     讲稿: %s' % left.strip()[:58])
        print('     目标: %s' % right.strip()[:64])
    return pages


def check_cursor(base, name):
    hr('%s  ③ 光标定位质量' % name)
    mid_p = os.path.join(base, 'cursor_mid.json')
    if not os.path.exists(mid_p):
        print('   ⚠️ cursor_mid.json 不存在（无法核验落点来源）'); return
    mid = json.load(open(mid_p, encoding='utf-8'))
    src_cnt = collections.Counter(it.get('source', '?') for it in mid)
    print('   条目数    : %d' % len(mid))
    print('   定位来源  : %s' % dict(src_cnt))

    # 落点去重
    cnt = collections.Counter((it['slide'], tuple(it['cursor'])) for it in mid)
    rep = {k: v for k, v in cnt.items() if v > 1}
    print('   落点去重  : %d 个条目落在 %d 个不同位置，重复位置 %d 个' % (
        len(mid), len(cnt), len(rep)))

    # 准确性：落点是否落在「所引用短语」的矩形内
    import cursor_gen as cg
    loc = cg.PdfSlideLocator(os.path.join(base, 'slides_rendered.pdf'), 2016, 1512)
    exact = inline = far = none = 0
    for it in mid:
        page = loc.doc[it['slide']]
        sx, sy = 2016.0 / page.rect.width, 1512.0 / page.rect.height
        phrase = cg.extract_quote(it['cursor_prompt']) or ''
        px, py = it['cursor']
        qx, qy = px / sx, py / sy
        rects = page.search_for(phrase) if phrase else []
        if rects and any(r.x0 - 8 <= qx <= r.x1 + 8 and r.y0 - 8 <= qy <= r.y1 + 8
                         for r in rects):
            exact += 1
            continue
        nt = cg.normalize_text(phrase)
        hit = False
        for bbox, text in loc._lines(it['slide']):
            if nt and nt in cg.normalize_text(text):
                x0, y0, x1, y1 = bbox
                hit = (x0 - 8 <= qx <= x1 + 8 and y0 - 8 <= qy <= y1 + 8)
                break
        if hit:
            inline += 1
        elif rects:
            far += 1
        else:
            none += 1
    loc.close()
    tot = len(mid)
    print()
    print('   落点核验  :')
    print('     ① 落在引用短语矩形内 : %d' % exact)
    print('     ② 落在含该短语的行内 : %d' % inline)
    print('     ③ 有短语但落点偏出   : %d' % far)
    print('     ④ 短语在本页找不到   : %d  （多为回退 grounding 的条目）' % none)
    print('     => 可核验的落点准确率 : %d/%d = %.0f%%' % (
        exact + inline, tot, 100.0 * (exact + inline) / max(tot, 1)))


def check_talking(base, name):
    hr('%s  ④ 数字人（MuseTalk）' % name)
    tdir = os.path.join(base, 'talking_musetalk')
    adir = os.path.join(base, 'audio')
    if not os.path.isdir(tdir):
        print('   ⚠️ talking_musetalk/ 不存在'); return
    idxs = sorted((d for d in os.listdir(tdir) if d.isdigit()), key=int)
    print('   页数      : %d' % len(idxs))
    n_audio = len([f for f in os.listdir(adir) if f.endswith('.wav')]) if os.path.isdir(adir) else 0
    print('   音频数    : %d  %s' % (n_audio, '✔ 一致' if n_audio == len(idxs) else '⚠️ 不一致'))

    # 逐页时长 vs 音频时长
    bad = 0
    rows = []
    for d in idxs:
        vp = os.path.join(tdir, d, 'portrait', 'merge_video.mp4')
        ap = os.path.join(adir, '%s.wav' % d)
        if not os.path.exists(vp):
            rows.append((d, None, wav_dur(ap) if os.path.exists(ap) else None))
            continue
        vd = float(probe(vp)['format']['duration'])
        ad = wav_dur(ap) if os.path.exists(ap) else None
        if ad is not None and abs(vd - ad) > 0.5:
            bad += 1
        rows.append((d, vd, ad))
    print('   逐页时长  : 视频 vs 音频，偏差 >0.5s 的有 %d 页' % bad)
    for d, vd, ad in rows[:6]:
        print('     第%-3s 视频 %s  音频 %s' % (
            d, '%.2fs' % vd if vd else '缺失', '%.2fs' % ad if ad else '缺失'))

    # 口型运动：不预设「嘴在哪」，而是找出**帧间变化的像素集中在哪**
    # —— 这样测出来的结论不依赖我对人脸位置的猜测。
    if FFPROBE:
        from shutil import which
        ff = which('ffmpeg')
        if ff and rows and rows[0][1]:
            vp = os.path.join(tdir, rows[0][0], 'portrait', 'merge_video.mp4')
            tmp = os.path.join(ROOT, '.workbuddy', 'logs', '_lipchk')
            os.makedirs(tmp, exist_ok=True)
            for f in os.listdir(tmp):
                os.remove(os.path.join(tmp, f))
            from PIL import Image
            n = 14
            frames = []
            for i in range(n):
                dst = os.path.join(tmp, 'f%02d.png' % i)
                subprocess.run([ff, '-y', '-v', 'error', '-ss', str(0.5 + i * 0.7),
                                '-i', vp, '-frames:v', '1', dst], check=True)
                frames.append(np.asarray(Image.open(dst).convert('L'), dtype=np.float32))
            H, W = frames[0].shape
            dmap = np.mean([np.abs(frames[i] - frames[i + 1])
                            for i in range(len(frames) - 1)], axis=0)
            print()
            print('   口型运动（第 0 页 %dx%d，%d 帧采样）:' % (W, H, n))
            print('     全画面平均帧间变化 : %.3f' % dmap.mean())
            print('     最大单点变化       : %.1f' % dmap.max())
            # 变化像素的空间分布：取超过全画面均值 3 倍的像素，求其包围盒
            thr = max(dmap.mean() * 3, 1.0)
            ys, xs = np.nonzero(dmap > thr)
            if len(xs) < 20:
                print('     ⚠️ 变化像素过少（%d 个），可能没在动' % len(xs))
            else:
                x0, x1, y0, y1 = xs.min(), xs.max(), ys.min(), ys.max()
                print('     显著变化像素       : %d 个（%.2f%% 画面），包围盒 x %d..%d  y %d..%d'
                      % (len(xs), 100.0 * len(xs) / (W * H), x0, x1, y0, y1))
                print('     包围盒中心         : 画面横向 %.0f%% / 纵向 %.0f%%'
                      % (100.0 * (x0 + x1) / 2 / W, 100.0 * (y0 + y1) / 2 / H))
                # 盒内 vs 盒外的平均变化 —— 「只重绘一小块」的直接证据
                mask = np.zeros((H, W), bool)
                mask[max(0, y0 - 6):y1 + 7, max(0, x0 - 6):x1 + 7] = True
                print('     盒内平均变化       : %.3f' % dmap[mask].mean())
                print('     盒外平均变化       : %.3f   （≈0 即「画面其他部分未被改动」）'
                      % dmap[~mask].mean())
            # 清理抽帧临时文件
            for f in os.listdir(tmp):
                os.remove(os.path.join(tmp, f))


def make_overview(base, name, pages):
    from PIL import Image, ImageDraw
    d = os.path.join(base, 'slide_imgs')
    if not os.path.isdir(d):
        return
    ids = sorted((f for f in os.listdir(d) if f.endswith('.png')),
                 key=lambda x: int(''.join(c for c in x if c.isdigit())))
    W = 400
    tiles = []
    for f in ids:
        im = Image.open(os.path.join(d, f)).convert('RGB')
        tiles.append(im.resize((W, int(im.height * W / im.width)), Image.LANCZOS))
    if not tiles:
        return
    cols = 4
    rows = (len(tiles) + cols - 1) // cols
    th, gap, head = tiles[0].height, 10, 26
    sheet = Image.new('RGB', (cols * W + (cols + 1) * gap,
                              rows * (th + head) + (rows + 1) * gap), 'white')
    dr = ImageDraw.Draw(sheet)
    for i, t in enumerate(tiles):
        r, c = divmod(i, cols)
        x = gap + c * (W + gap)
        y = gap + r * (th + head + gap)
        sheet.paste(t, (x, y))
        dr.text((x, y + th + 6), 'slide %d' % (i + 1), fill=(40, 40, 40))
    out = os.path.join(base, 'slides_overview.png')
    sheet.save(out)
    print('   → 幻灯片总览: %s  %s' % (out, sheet.size))


def main():
    # 可选：只验指定组合（便于某个先跑完时先验它）
    #     python scripts/verify_gkk_3combos.py gkk_en
    names = [a for a in sys.argv[1:] if a in COMBOS] or COMBOS
    print('  ffprobe:', FFPROBE)
    print('  根目录  :', ROOT)
    print('  待验组合:', names)
    ok = []
    for name in names:
        base = os.path.join(ROOT, 'result', name)
        if not os.path.isdir(base):
            hr('%s  缺失' % name)
            print('   ⚠️ result/%s 不存在' % name)
            ok.append((name, False))
            continue
        print()
        print('#' * 70)
        print('#  %s   （%s）' % (name, LABEL.get(name, '')))
        print('#' * 70)
        check_merge(base, name)
        pages = check_script(base, name)
        check_cursor(base, name)
        check_talking(base, name)
        hr('%s  ⑤ 幻灯片总览' % name)
        make_overview(base, name, pages)
        ok.append((name, True))
    hr('汇总')
    for name, present in ok:
        print('   %-10s %s' % (name, '产物存在' if present else '⚠️ 缺失'))
    return 0


if __name__ == '__main__':
    sys.exit(main())
