"""编译通过之外的课件质量门禁：裁切及源图绑定，纯本地检查。"""
import json
import re
import hashlib
from pathlib import Path
from spoken_text import plain_text

QUALITY_VERSION = 5

LAYOUT_PROMPT = ('\n[Slide quality requirements] One idea per slide; at most five short bullet points. '
                 'Split dense content into separate frames before reducing image size. '
                 'Never hide overflow with clipping, resizebox or tiny fonts. '
                 'Images must use width<=0.9\\linewidth, height<=0.5\\textheight,keepaspectratio. '
                 'Every \\caption must be inside a figure or table environment; '
                 'never place a bare caption in a frame or column. '
                 'In tabularray rowspec, concatenate row types without separating commas, '
                 'for example rowspec={Q[m]Q[m]}; commas inside Q[...] options are allowed. '
                 'Preserve source figure IDs and captions. No unfulfilled promises of live demos.')


def bound_image_sizes(code):
    """为普通 includegraphics 补齐双向尺寸约束；不缩小文字、不裁剪图片。

    高度预留标题和图注空间。已有更小的相对尺寸保留，特殊尺寸表达式交给
    编译门禁和模型修复，避免猜测其含义。
    """
    def bound(match):
        options = [part.strip() for part in (match[1] or '').split(',') if part.strip()]
        bounded = []
        seen = set()
        for option in options:
            name, sep, value = option.partition('=')
            name = name.strip()
            if name in ('width', 'height') and sep:
                seen.add(name)
                size = re.fullmatch(r'((?:\d+(?:\.\d*)?|\.\d+)?)\\(textwidth|linewidth|columnwidth|textheight)', value.strip())
                if size and ((name == 'width' and size[2] != 'textheight')
                             or (name == 'height' and size[2] == 'textheight')):
                    limit = 0.9 if name == 'width' else 0.45
                    number = min(float(size[1] or '1'), limit)
                    unit = 'linewidth' if name == 'width' else 'textheight'
                    option = '{}={:g}\\{}'.format(name, number, unit)
            if name != 'keepaspectratio':
                bounded.append(option)
        if 'width' not in seen:
            bounded.append(r'width=0.9\linewidth')
        if 'height' not in seen:
            bounded.append(r'height=0.45\textheight')
        bounded.append('keepaspectratio')
        return '\\includegraphics[' + ','.join(bounded) + ']{' + match[2] + '}'
    return re.sub(r'\\includegraphics(?:\s*\[([^\]]*)\])?\s*\{([^}]*)\}', bound, code)


def _sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def normalize_source_bindings(code):
    """只迁移紧邻 figure 前的现有标记，不推断或改写图号。"""
    return re.sub(
        r'(?m)^([ \t]*)(%[ \t]*source-(?:figure|page):[^\r\n]*)\r?\n'
        r'[ \t]*(\\begin\{figure\}(?:\[[^\]\r\n]*\])?)',
        lambda match: match[1] + match[3] + '\n' + match[1] + match[2], code)


def normalize_rowspec(code):
    """只删除简单 Q[...] 行类型之间的非法逗号，不改选项、宏或表格内容。"""
    def repair(match):
        prefix = code[code.rfind('\n', 0, match.start()) + 1:match.start()]
        if re.search(r'(?<!\\)(?:\\\\)*%', prefix):
            return match[0]  # 注释中的示例保持原样。
        spec = re.sub(r'(\]\s*),(?=\s*Q\[)', r'\1', match['spec'])
        return match['head'] + spec + '}'

    # 不处理嵌套花括号、宏或自定义行类型，以免改变未知语义。
    return re.sub(
        r'(?<![\\\w])(?P<head>rowspec\s*=\s*\{)'
        r'(?P<spec>\s*Q\[[^\[\]{}%]*\](?:\s*,?\s*Q\[[^\[\]{}%]*\])*\s*)\}',
        repair, code)


def _caption_numbers(text):
    return [re.sub(r'\s+', '', value).replace('–', '-').replace('－', '-')
            for value in re.findall(r'(?:图|Fig(?:ure)?\.?)\s*(\d+(?:\s*[-–－]\s*\d+)?)', text, re.I)]


def quality_errors(code, feedback='', figures=None, expected_count=None):
    code = normalize_source_bindings(code)
    errors = ['版面裁切：'+line.strip() for line in feedback.splitlines() if re.search(r'Overfull \\[hv]box',line,re.I)]
    errors += ['字体缺字：'+line.strip() for line in feedback.splitlines()
               if 'could not represent character' in line and '"nullfont"' not in line]
    if expected_count is not None:
        count = len(re.findall(r'\\begin\{frame\}', code))
        if count != expected_count:
            errors.append('页数不符：要求{}页，实际{}帧；请合并或拆分主题，保留源事实与原图注'.format(expected_count, count))
    if figures is not None:
        known = {fig['rel'].replace('\\','/'):fig for fig in figures}
        for frame_no, frame in enumerate(re.findall(r'\\begin\{frame\}.*?\\end\{frame\}',code,re.S),1):
            title_match = re.search(r'\\begin\{frame\}(?:\[[^\]]*\])?\{([^{}]*)\}',frame)
            title = title_match[1] if title_match else ''
            for match in re.finditer(r'\\includegraphics(?:\[[^\]]*\])?\{([^}]+)\}',frame):
                name = match[1].replace('\\','/')
                fig = known.get(name)
                if name.startswith('figures/') and fig is None:
                    errors.append('第{}帧引用清单外图片 {}'.format(frame_no,name))
                    continue
                if not fig:
                    continue
                if re.search(r'神经网络|网络结构|neural network',fig.get('caption',''),re.I) and re.search(r'山谷|斜坡|滚下|valley',title,re.I):
                    errors.append('第{}帧网络结构图不能用于山谷/斜坡示意，请选择对应源图'.format(frame_no))
                contexts = [block for block in re.findall(r'\\begin\{figure\}.*?\\end\{figure\}',frame,re.S) if match[0] in block]
                context = contexts[0] if contexts else frame
                fid = fig.get('figure_id')
                if fid:
                    number = fid[1:]
                    captions = re.findall(r'\\caption(?:\[[^\]]*\])?\{([^{}]*)\}',context)
                    labels = _caption_numbers(' '.join(captions))
                    source_labels = _caption_numbers(fig.get('caption', ''))
                    bindings = re.findall(r'%[ \t]*source-figure:[ \t]*(F\d+(?:-\d+)?)(?=[ \t\r\n]|$)', context)
                    if bindings != [fid] or number not in labels or labels != source_labels:
                        errors.append('第{}帧 {} 必须在 figure 内绑定 {}，原图号应为 {}，实际图号为 {}，绑定为 {}；禁止图号错配'.format(
                            frame_no, name, fid, '/'.join(source_labels), '/'.join(labels) or '未找到', '/'.join(bindings) or '未找到'))
                    if not any(plain_text(re.sub(r'\\[A-Za-z]+','',caption)) == plain_text(fig['caption']) for caption in captions):
                        errors.append('第{}帧 {} 图注与原文不一致；必须保留完整原图注 {}'.format(frame_no,name,fig['caption']))
                elif fig.get('page') is not None and not re.search(r'%\s*source-page:\s*'+str(fig['page'])+r'\b',context):
                    errors.append('第{}帧无图号图片 {} 须注明 source-page，不能猜图注'.format(frame_no,name))
    return errors


def record_quality(tex_path, feedback, compile_ok, expected_count=None):
    target = Path(tex_path)
    code = target.read_text(encoding='utf-8')
    index = target.parent/'figures/index.json'
    figures = json.loads(index.read_text(encoding='utf-8')).get('figures',[]) if index.is_file() else []
    errors = quality_errors(code,feedback,figures,expected_count) if re.search(r'\\documentclass(?:\[[^\]]*\])?\{(?:ctex)?beamer\}',code) else []
    if compile_ok and expected_count is not None:
        import pymupdf
        with pymupdf.open(target.with_suffix('.pdf')) as pdf:
            if len(pdf) != expected_count:
                errors.append('页数不符：要求{}页，渲染PDF实际{}页；移除额外逐步展开或合并主题'.format(expected_count, len(pdf)))
    report = {'version':QUALITY_VERSION,'tex_sha256':_sha(target),'figures_sha256':_sha(index),'pdf_sha256':_sha(target.with_suffix('.pdf')),
              'expected_slide_count':expected_count,
              'compile_ok':compile_ok,'passed':compile_ok and not errors,'errors':errors}
    target.with_suffix('.quality.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    return errors


def require_quality(project_root):
    root = Path(project_root)
    tex = root/'slides_refined.tex'
    if not tex.is_file():
        tex = root/'slides.tex'
    if not tex.is_file():
        return
    report_path = tex.with_suffix('.quality.json')
    report = json.loads(report_path.read_text(encoding='utf-8')) if report_path.is_file() else {}
    if report.get('version') != QUALITY_VERSION or report.get('tex_sha256') != _sha(tex) or report.get('figures_sha256') != _sha(root/'figures/index.json') or report.get('pdf_sha256') != _sha(tex.with_suffix('.pdf')):
        from slide_code_gen_select_improvement import compile_tex
        expected = report.get('expected_slide_count')
        ok, feedback = compile_tex(tex, **({'expected_count': expected} if expected is not None else {}))
        if not ok:
            raise ValueError('课件未通过质量门禁，收费前停止：'+feedback[-2000:])
    elif not report.get('passed'):
        raise ValueError('课件未通过质量门禁，收费前停止：'+str(report.get('errors')))
