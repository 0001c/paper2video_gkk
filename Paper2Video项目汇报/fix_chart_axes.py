"""修掉横向条形图的轴互换问题。

根因：转换器写 <c:axPos> 时只按「柱状图」那套写死
      catAx=b(下) / valAx=l(左)，完全没看 barDir。
      对 barDir="col"（竖向柱）恰好正确；
      对 barDir="bar"（横向条）必须反过来 —— catAx=l(左) / valAx=b(下)。

修法：**只交换这两个 axPos 值**，不动任何其它 XML（最小改动）。
      不补 <c:crosses>：三张图都没写（含渲染正常的那张），说明这个渲染器不需要它，
      贸然补反而有搞错元素顺序的风险。

同时做备份。脚本可重复执行（已正确则跳过）。
"""
import io
import os
import re
import shutil
import time
import zipfile

PPTX = 'Paper2Video项目汇报.pptx'
BACKUP_DIR = 'backup'
TARGETS = ['ppt/slides/charts/chart1.xml', 'ppt/slides/charts/chart2.xml']

# 期望值：barDir -> (catAx 应在哪, valAx 应在哪)
WANT = {'bar': ('l', 'b'), 'col': ('b', 'l')}


def patch_axis(xml, kind, want):
    """把某个轴块里的 axPos 改成 want，返回 (新 XML, 是否改动, 原值)。"""
    m = re.search(r'<c:%s>(.*?)</c:%s>' % (kind, kind), xml, re.S)
    if not m:
        raise RuntimeError('找不到 <%s> 元素' % kind)
    block = m.group(1)
    cur = re.findall(r'<c:axPos val="(\w+)"/>', block)
    if len(cur) != 1:
        raise RuntimeError('<%s> 里的 axPos 有 %d 个（应为 1）' % (kind, len(cur)))
    if cur[0] == want:
        return xml, False, cur[0]
    newblock = block.replace('<c:axPos val="%s"/>' % cur[0],
                             '<c:axPos val="%s"/>' % want, 1)
    start, end = m.start(1), m.end(1)
    return xml[:start] + newblock + xml[end:], True, cur[0]


print("=== ① 备份 ===")
os.makedirs(BACKUP_DIR, exist_ok=True)
if not os.path.isfile(PPTX):
    raise SystemExit('✘ 找不到 %s' % PPTX)
stamp = time.strftime('%Y%m%d_%H%M%S')
bak = os.path.join(BACKUP_DIR, 'Paper2Video项目汇报_%s.pptx' % stamp)
shutil.copy2(PPTX, bak)
print("   %s  ->  %s（%.1f MB）" % (PPTX, bak, os.path.getsize(bak) / 1024 / 1024))

print()
print("=== ② 逐个图表检查并按需修 ===")
zin = zipfile.ZipFile(PPTX)
entries = {n: zin.read(n) for n in zin.namelist()}
infos = zin.infolist()
zin.close()

changed = []
for target in TARGETS:
    if target not in entries:
        print("   ✘ %s 不存在" % target)
        continue
    xml = entries[target].decode('utf-8')
    bar = re.findall(r'<c:barDir val="(\w+)"', xml)
    if not bar:
        print("   %s 没有 barDir，跳过" % target)
        continue
    bar = bar[0]
    if bar not in WANT:
        print("   %s barDir=%s 不在已知范围，跳过" % (target, bar))
        continue
    cat_want, val_want = WANT[bar]
    print("   %s  barDir=%s → 期望 catAx=%s / valAx=%s" % (
        target.split('/')[-1], bar, cat_want, val_want))

    xml2, c1, old1 = patch_axis(xml, 'catAx', cat_want)
    xml2, c2, old2 = patch_axis(xml2, 'valAx', val_want)
    if not (c1 or c2):
        print("      已经正确，跳过（catAx=%s, valAx=%s）" % (old1, old2))
        continue
    print("      catAx %s -> %s   valAx %s -> %s" % (old1, cat_want, old2, val_want))
    entries[target] = xml2.encode('utf-8')
    changed.append(target)

if not changed:
    print("\n   无需改动。")
    raise SystemExit(0)

print()
print("=== ③ 写回 pptx（保留全部条目）===")
tmp = PPTX + '.tmp'
with zipfile.ZipFile(tmp, 'w', zipfile.ZIP_DEFLATED) as zout:
    for info in infos:
        zout.writestr(info, entries[info.filename])
os.replace(tmp, PPTX)
print("   已写入 %s（%.1f MB）" % (PPTX, os.path.getsize(PPTX) / 1024 / 1024))

print()
print("=== ④ 验证 ===")
z = zipfile.ZipFile(PPTX)
bad = z.testzip()
print("   zip 完整性     : %s" % ('✔ 通过' if bad is None else '✘ 损坏于 ' + bad))
n_slides = len([n for n in z.namelist()
                if re.match(r'ppt/slides/slide\d+\.xml$', n)])
print("   页数           : %d" % n_slides)
print("   条目数         : %d（应保持不变）" % len(z.namelist()))
import xml.dom.minidom as _md
for target in TARGETS:
    xml = z.read(target).decode('utf-8')
    try:
        _md.parseString(xml)
        wf = '✔ XML 合法'
    except Exception as exc:
        wf = '✘ XML 非法：%s' % exc
    bar = re.findall(r'<c:barDir val="(\w+)"', xml)[0]
    cat = re.findall(r'<c:catAx>.*?<c:axPos val="(\w+)"',
                     xml, re.S)
    val = re.findall(r'<c:valAx>.*?<c:axPos val="(\w+)"',
                     xml, re.S)
    want = WANT[bar]
    ok = (cat and val and cat[0] == want[0] and val[0] == want[1])
    print("   %-14s barDir=%-4s catAx=%-2s valAx=%-2s  %s  %s" % (
        target.split('/')[-1], bar, cat[0] if cat else '?',
        val[0] if val else '?', wf, '✔ 轴位正确' if ok else '✘ 仍不对'))
z.close()

print()
print("=== ⑤ 用 pptx 库复开一次（若能装到）===")
try:
    from pptx import Presentation
    p = Presentation(PPTX)
    print("   ✔ python-pptx 打开成功，页数 = %d" % len(p.slides))
except ImportError:
    print("   （本机没有 python-pptx，跳过 —— 已用 zip 完整性 + XML 合法性替代）")
except Exception as exc:
    print("   ✘ 打开失败：%s" % exc)
