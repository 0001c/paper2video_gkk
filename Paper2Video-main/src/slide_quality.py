"""编译通过之外的课件质量门禁：裁切及源图绑定，纯本地检查。"""
import json
import re
import hashlib
from pathlib import Path
from spoken_text import plain_text

LAYOUT_PROMPT = ('\n[Slide quality requirements] One idea per slide; at most five short bullet points. '
                 'Split dense content into separate frames before reducing image size. '
                 'Never hide overflow with clipping, resizebox or tiny fonts. '
                 'Images must use width<=0.9\\linewidth, height<=0.5\\textheight,keepaspectratio. '
                 'Preserve source figure IDs and captions. No unfulfilled promises of live demos.')


def _sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def quality_errors(code, feedback='', figures=None):
    errors = ['版面裁切：'+line.strip() for line in feedback.splitlines() if re.search(r'Overfull \\[hv]box',line,re.I)]
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
                    labels = re.findall(r'(?:图|Fig(?:ure)?\.?)\s*(\d+(?:\s*[-–－]\s*\d+)?)',' '.join(captions),re.I)
                    labels = [re.sub(r'\s+','',x).replace('–','-').replace('－','-') for x in labels]
                    if not re.search(r'%\s*source-figure:\s*'+re.escape(fid)+r'(?![\d-])',context) or number not in labels or any(label != number for label in labels):
                        errors.append('第{}帧 {} 必须绑定 {} 并保留对应原图号，禁止图号错配'.format(frame_no,name,fid))
                    if not any(plain_text(re.sub(r'\\[A-Za-z]+','',caption)) == plain_text(fig['caption']) for caption in captions):
                        errors.append('第{}帧 {} 图注与原文不一致；必须保留完整原图注 {}'.format(frame_no,name,fig['caption']))
                elif not re.search(r'%\s*source-page:\s*'+str(fig.get('page'))+r'\b',context):
                    errors.append('第{}帧无图号图片 {} 须注明 source-page，不能猜图注'.format(frame_no,name))
    return errors


def record_quality(tex_path, feedback, compile_ok):
    target = Path(tex_path)
    code = target.read_text(encoding='utf-8')
    index = target.parent/'figures/index.json'
    figures = json.loads(index.read_text(encoding='utf-8')).get('figures',[]) if index.is_file() else []
    errors = quality_errors(code,feedback,figures) if re.search(r'\\documentclass(?:\[[^\]]*\])?\{(?:ctex)?beamer\}',code) else []
    report = {'version':2,'tex_sha256':_sha(target),'figures_sha256':_sha(index),'pdf_sha256':_sha(target.with_suffix('.pdf')),
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
    if report.get('version') != 2 or report.get('tex_sha256') != _sha(tex) or report.get('figures_sha256') != _sha(root/'figures/index.json') or report.get('pdf_sha256') != _sha(tex.with_suffix('.pdf')):
        from slide_code_gen_select_improvement import compile_tex
        ok, feedback = compile_tex(tex)
        if not ok:
            raise ValueError('课件未通过质量门禁，收费前停止：'+feedback[-2000:])
    elif not report.get('passed'):
        raise ValueError('课件未通过质量门禁，收费前停止：'+str(report.get('errors')))
