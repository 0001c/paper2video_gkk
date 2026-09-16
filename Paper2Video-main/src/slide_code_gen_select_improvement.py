'''
    Slide Beamer Code Generation
'''

import re
import fitz
import yaml
import json
import bisect
import string
import os, sys, pdb
import subprocess
import multiprocessing as mp
from os import path
from pathlib import Path
from bisect import bisect_right
from camel.models import ModelFactory
from camel.agents import ChatAgent
from camel.messages import BaseMessage
from camel.types import ModelPlatformType
from pathlib import Path
from typing import Sequence, Tuple, Optional
from PIL import Image, ImageDraw, ImageFont
from wei_utils import get_agent_config


def extract_json_block(text: str, first_only: bool = True):
    pattern = r"```json\s*([\s\S]*?)\s*```"
    matches = re.findall(pattern, text, flags=re.IGNORECASE)
    if first_only:
        return matches[0] if matches else text
    return matches

def extract_beamer_code(tex_str):
    match = re.search(r"(\\documentclass(?:\[[^\]]*\])?\{beamer\}.*?\\end\{document\})", tex_str, re.DOTALL)
    return match.group(1) if match else None

def latex_code_gen_upgrade(prompt_path, tex_dir, beamer_save_path, 
                           model_config_ll, model_config_vl,
                           beamer_temp_name=None, if_fix=True, if_tree_search=True):
    model = ModelFactory.create(
        model_platform=model_config_ll["model_platform"],
        model_type=model_config_ll["model_type"],
        model_config_dict=model_config_ll.get("model_config"),
        url=model_config_ll.get("url", None),)
    agent = ChatAgent(model=model, system_message="",)
    with open(prompt_path, 'r', encoding='utf-8') as f_prompt: templete_prompt = f_prompt.read()
    token_usage = {}
    ## paper latex code input
    tex_list = find_all_tex_files(tex_dir)
    tex_content = '/n'.join(tex_list) # paper latex
    root_dir = Path(tex_dir) 
    all_relative_paths = [str(file.relative_to(root_dir)) for file in root_dir.rglob("*") if file.is_file()] # figure path

    ## slide code generation
    if beamer_temp_name is None:
        main_inference_prompt = [
            templete_prompt, "This is the latex code for paper:", tex_content,
            "The file pathes in the project are: \n{}".format(str(all_relative_paths))
        ]
    else:
        main_inference_prompt = [
            templete_prompt, "This is the latex code for paper:", tex_content,
            "The file pathes in the project are: \n{}".format(str(all_relative_paths)),
            "Use Beamer Theme: {}".format(beamer_temp_name) 
        ]
        
    main_inference_prompt = "\n".join(map(str, main_inference_prompt))
    user_msg = BaseMessage.make_user_message(role_name="User", content=main_inference_prompt)
    response = safe_step(agent, user_msg)
    token_usage["slide_gen"] = response.info['usage']
    
    code = extract_beamer_code(response.msgs[-1].content)
    if not isinstance(code, str) or not code.strip():
        # 不能带着 None 往下走：下面那句 open(path,"w") 会**先把文件截断成 0 字节**，
        # 再因为 write(None) 抛 TypeError。结果是「代码没生成出来」反而把上一版
        # 已经能编译的 slides.tex 毁掉。这里直接抛错，保住磁盘上的旧文件。
        raise RuntimeError(
            "幻灯片代码生成失败：模型返回中未找到可用的 beamer 代码。原始返回：\n{}".format(
                response.msgs[-1].content[:2000]))
    with open(beamer_save_path, "w", encoding="utf-8") as f: f.write(code)
    ok, feedback = compile_tex(beamer_save_path)

    ## fix if error
    num_try = 0
    token_usage["fix"] = []
    while num_try < 10 and not ok:
        error_info = re.findall(r'^(error: .+)', feedback, flags=re.MULTILINE)
        if not error_info:
            # 编译失败但拿不到 "error: " 行，说明失败原因不在反馈文本里（例如
            # 缺 DLL、tectonic 崩溃）。继续让模型"盲修"只会浪费 token。
            print("[slide_gen] 编译失败但未解析到 'error: ' 行，停止修复")
            break
        agent.reset()
        fixed_code, fix_usage = correcte_error(code, error_info, agent)
        token_usage["fix"].append(fix_usage)
        if not isinstance(fixed_code, str) or not fixed_code.strip():
            # 同上：宁可停在上一版，也绝不把代码写成 None 后再截断文件。
            print("[slide_gen] 修复未返回可用代码，保留上一版并停止修复")
            break
        code = fixed_code
        with open(beamer_save_path, "w", encoding="utf-8") as f: f.write(code)
        ok, feedback = compile_tex(beamer_save_path)
        num_try += 1

    ## improve slide layout
    config = model_config_vl
    if if_tree_search is True:
        new_code_save_path, token_usage_improve = improve_layout(code, feedback, beamer_save_path, config)
        token_usage["improve"] = token_usage_improve
        return token_usage, new_code_save_path
    else:
        return token_usage, beamer_save_path.replace(".tex", ".pdf")

select_proposal_prompt_path = "prompts/select_proposal.txt"
def improve_layout(code, feedback, beamer_save_path, model_config):
    with open(select_proposal_prompt_path, 'r') as f: template_prompt = f.read()
    token_usage_improve = []
    
    ## get layout warning info
    warning_info = re.findall(r'^(warning: .+)', feedback, flags=re.MULTILINE)
    warning_info = warning_info[:len(warning_info)//2]
    warning_info = [s for s in warning_info if 'Overfull' in s]
    
    ## find out which slide needed to be improved
    head = re.search(r'\\documentclass(?:\[[^\]]*\])?\{beamer\}(.*?)\\begin{document}', code, flags=re.DOTALL).group(1)
    head = head + "\n" + "\\setbeamerfont{caption}{size=\\scriptsize}" ## smaller the caption front size
    frames = compute_frame_spans(code)
    need_improve_list = []
    for warning in warning_info:
        num = int(re.search(r'(?<=\.tex:)\d+', warning).group())
        for idx, f in enumerate(frames):
            if f["start_line"]<=num<= f["end_line"]:
                if "\\includegraphics" in f["text"]:
                    need_improve_list.append(idx)
                break
    need_improve_list = sorted(set(need_improve_list))
    ## propose
    # num_process = 4
    # args_list = []
    # for idx, frame_idx in enumerate(need_improve_list):
    #     args_list.append([idx, model_config, template_prompt, head, frames[frame_idx]])
    # with mp.Pool(processes=num_process) as pool: results = pool.map(improve_per_slide, args_list)
    # for result in results:
    #     idx, refined_code, usage_improve = result
    #     frames[frame_idx]["text"] = refined_code
    #     token_usage_improve.append(usage_improve)
    imporve_model = ModelFactory.create(
        model_platform=model_config["model_platform"],
        model_type=model_config["model_type"],
        model_config_dict=model_config.get("model_config"),
        url=model_config.get("url", None),)
    imporve_agent = ChatAgent(model=imporve_model, system_message="",)
    proposal_tmp_dir = path.join(path.dirname(beamer_save_path), 'proposal_imgs')
    os.makedirs(proposal_tmp_dir, exist_ok=True)
    factors = [1, 0.75, 0.5, 0.25]
    map_dic = {"A": 0, "B": 1, "C": 2, "D": 3}
    for idx, frame_idx in enumerate(need_improve_list):
        frame = frames[frame_idx]
        proposal_imgs_path_list = []
        proposal_code_list = []
        for factor in factors:
            proposal_code = scale_includegraphics_widths(frame["text"], factor)
            proposal_code = add_small_after_blocks(proposal_code)
            proposal_full_code =  '\n'.join(["\\documentclass{beamer}", head, "\\begin{document}", proposal_code, "\\end{document}"])
            proposal_code_save_path = beamer_save_path.replace('.tex', 'proposal_{}.tex'.format(str(factor)))
            with open(proposal_code_save_path, 'w') as f: f.write(proposal_full_code)
            _, feedback = compile_tex(proposal_code_save_path)  
            img_path = pdf2img(proposal_code_save_path.replace(".tex", ".pdf"), proposal_tmp_dir)
            proposal_imgs_path_list.append(img_path)  
            proposal_code_list.append(proposal_code)
        prompt_img_path =  path.join(proposal_tmp_dir, "meraged.png")
        make_grid_with_labels(proposal_imgs_path_list, prompt_img_path, rows=2, cols=2)
        imporve_agent.reset() # inference
        user_msg = BaseMessage.make_user_message(
                role_name="User",
                content="\n".join([template_prompt, "Here are the choices A, B, C, D"]),
                image_list=[Image.open(prompt_img_path)]
        )
        response = safe_step(imporve_agent, user_msg)
        token_usage_improve.append(response.info['usage'])
        print(response.msgs[-1].content)
        choice_str = extract_json_block(response.msgs[-1].content)
        print(choice_str)
        choice = json.loads(choice_str)
        refined_code = proposal_code_list[map_dic[choice["choice"]]]
        frames[frame_idx]["text"] = refined_code
    ## update code
    new_code = ["\\documentclass{beamer}", head, "\\begin{document}"]
    section = []
    subsection = []
    for frame in frames: 
        if len(frame["section"]) != 0 and frame["section"] not in section:  
            new_code.append("\\section{{{}}}".format(frame["section"]))
            section.append(frame["section"])
            subsection = []
        if len(frame["subsection"]) != 0 and frame["subsection"] not in subsection: 
            new_code.append("\\subsection{{{}}}".format(frame["subsection"]))
            subsection.append(frame["subsection"])
        new_code.append(add_small_after_blocks(frame["text"]))   
    new_code.append("\\end{document}")
    new_code = "\n".join(new_code)
    new_code_save_path = beamer_save_path.replace(".tex", "_refined.tex")
    with open(new_code_save_path, 'w') as f: f.write(new_code) 
    _, feedback = compile_tex(new_code_save_path)
    return new_code_save_path.replace(".tex", ".pdf"), token_usage_improve

def improve_per_slide(data):
    idx, model_config, template_prompt, head, frame = data
    ## model for selecting the proposed result
    imporve_model = ModelFactory.create(
        model_platform=model_config["model_platform"],
        model_type=model_config["model_type"],
        model_config_dict=model_config.get("model_config"),
        url=model_config.get("url", None),)
    imporve_agent = ChatAgent(model=imporve_model, system_message="",)
    factors = [1, 0.75, 0.5, 0.25]
    map_dic = {"A": 0, "B": 1, "C": 2, "D": 3}
    proposal_tmp_dir = path.join(path.dirname(beamer_save_path), 'proposal_imgs_'+str(idx))
    os.makedirs(proposal_tmp_dir, exist_ok=True)
    proposal_imgs_path_list = []
    proposal_code_list = []
    for factor in factors:
        proposal_code = scale_includegraphics_widths(frame["text"], factor)
        proposal_code = add_small_after_blocks(proposal_code)
        proposal_full_code =  '\n'.join(["\\documentclass{beamer}", head, "\\begin{document}", proposal_code, "\\end{document}"])
        proposal_code_save_path = beamer_save_path.replace('.tex', 'proposal_{}.tex'.format(str(factor)))
        with open(proposal_code_save_path, 'w') as f: f.write(proposal_full_code)
        _, feedback = compile_tex(proposal_code_save_path)
        img_path = pdf2img(proposal_code_save_path.replace(".tex", ".pdf"), proposal_tmp_dir)
        proposal_imgs_path_list.append(img_path)  
        proposal_code_list.append(proposal_code)
    prompt_img_path =  path.join(proposal_tmp_dir, "meraged.png")
    make_grid_with_labels(proposal_imgs_path_list, prompt_img_path, rows=2, cols=2)
    imporve_agent.reset() # inference
    user_msg = BaseMessage.make_user_message(
            role_name="User",
            content="\n".join([template_prompt, "Here are the choices A, B, C, D"]),
            image_list=[Image.open(prompt_img_path)]
    )
    response = safe_step(imporve_agent, user_msg)
    choice = json.loads(response.msgs[-1].content)
    refined_code = proposal_code_list[map_dic[choice["choice"]]]
    return idx, refined_code, response.info['usage']

def make_2x2_grid_with_labels(
    img_paths: Sequence[str],
    out_path: str,
    cell_size: Tuple[int, int] = (512, 512),
    gap: int = 16,
    labels: Sequence[str] = ("A", "B", "C", "D"),
    bg_color: Tuple[int, int, int] = (255, 255, 255),
    font_path: Optional[str] = None,
    font_size: Optional[int] = None,
) -> Path:

    if len(img_paths) != 4: raise ValueError("img_paths must contain 4 img pathes")

    cw, ch = cell_size
    canvas_w = cw * 2 + gap
    canvas_h = ch * 2 + gap
    canvas = Image.new("RGB", (canvas_w, canvas_h), bg_color)

    def _to_rgb(img: Image.Image) -> Image.Image:
        if img.mode in ("RGBA", "LA"):
            base = Image.new("RGB", img.size, bg_color)
            base.paste(img, mask=img.split()[-1])
            return base
        return img.convert("RGB")

    if font_size is None:
        font_size = max(16, int(min(cw, ch) * 0.08))
    font = None
    if font_path:
        try:
            font = ImageFont.truetype(font_path, font_size)
        except Exception:
            font = None
    if font is None:
        for try_name in ["DejaVuSans-Bold.ttf", "Arial.ttf", "Helvetica.ttf"]:
            try:
                font = ImageFont.truetype(try_name, font_size)
                break
            except Exception:
                continue
    if font is None:
        font = ImageFont.load_default()

    draw = ImageDraw.Draw(canvas)
    positions = [
        (0, 0),            # A
        (cw + gap, 0),     # B
        (0, ch + gap),     # C
        (cw + gap, ch + gap)  # D
    ]

    for i, (p, (x0, y0)) in enumerate(zip(img_paths, positions)):
        im = Image.open(p)
        im = _to_rgb(im)
        w, h = im.size
        scale = min(cw / w, ch / h)
        nw, nh = max(1, int(w * scale)), max(1, int(h * scale))
        im_resized = im.resize((nw, nh), Image.BICUBIC)
        px = x0 + (cw - nw) // 2
        py = y0 + (ch - nh) // 2
        canvas.paste(im_resized, (px, py))

        label = labels[i]
        margin = max(6, font_size // 4)
        tx, ty = x0 + margin, y0 + margin
        draw.text(
            (tx, ty), label, font=font,
            fill=(255, 255, 255),
            stroke_width=max(1, font_size // 16),
            stroke_fill=(0, 0, 0)
        )

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(out_path.as_posix())

def make_grid_with_labels(
    img_paths: Sequence[str],
    out_path: str,
    cell_size: Tuple[int, int] = (512, 512),
    gap: int = 16,
    rows: int = 2,
    cols: int = 3,
    labels: Optional[Sequence[str]] = None,     # 默认自动 A..Z
    bg_color: Tuple[int, int, int] = (255, 255, 255),
    font_path: Optional[str] = None,
    font_size: Optional[int] = None,
) -> Path:
    n = rows * cols
    if len(img_paths) != n:
        raise ValueError(f"img_paths must contain {n} image paths (got {len(img_paths)})")

    if labels is None:
        labels = list(string.ascii_uppercase[:n])
    elif len(labels) != n:
        raise ValueError(f"labels length must be {n} (got {len(labels)})")

    cw, ch = cell_size
    canvas_w = cw * cols + gap * (cols - 1)
    canvas_h = ch * rows + gap * (rows - 1)
    canvas = Image.new("RGB", (canvas_w, canvas_h), bg_color)

    def _to_rgb(img: Image.Image) -> Image.Image:
        if img.mode in ("RGBA", "LA"):
            base = Image.new("RGB", img.size, bg_color)
            base.paste(img, mask=img.split()[-1])
            return base
        return img.convert("RGB")

    if font_size is None:
        font_size = max(16, int(min(cw, ch) * 0.08))
    font = None
    if font_path:
        try:
            font = ImageFont.truetype(font_path, font_size)
        except Exception:
            font = None
    if font is None:
        for try_name in ["DejaVuSans-Bold.ttf", "Arial.ttf", "Helvetica.ttf"]:
            try:
                font = ImageFont.truetype(try_name, font_size)
                break
            except Exception:
                continue
    if font is None:
        font = ImageFont.load_default()

    draw = ImageDraw.Draw(canvas)

    positions = []
    for r in range(rows):
        for c in range(cols):
            x0 = c * (cw + gap)
            y0 = r * (ch + gap)
            positions.append((x0, y0))

    for i, (p, (x0, y0)) in enumerate(zip(img_paths, positions)):
        with Image.open(p) as im_raw:
            im = _to_rgb(im_raw)
        w, h = im.size
        scale = min(cw / w, ch / h)
        nw, nh = max(1, int(w * scale)), max(1, int(h * scale))
        im_resized = im.resize((nw, nh), Image.BICUBIC)

        px = x0 + (cw - nw) // 2
        py = y0 + (ch - nh) // 2
        canvas.paste(im_resized, (px, py))

        label = labels[i]
        margin = max(6, font_size // 4)
        tx, ty = x0 + margin, y0 + margin
        draw.text(
            (tx, ty), label, font=font,
            fill=(255, 0, 0),
            stroke_width=max(1, font_size // 16),
            stroke_fill=(255, 0, 0)
        )

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(out_path.as_posix())
    return out_path

def pdf2img(pdf_path, image_dir, dpi=300, fmt="png", strict_single_page=True):
    pdf_path = Path(pdf_path)
    image_dir = Path(image_dir)
    if pdf_path.suffix.lower() != ".pdf": raise ValueError(f"not pdf file: {pdf_path}")
    if not pdf_path.exists(): raise FileNotFoundError(f"can not find: {pdf_path}")
    with fitz.open(pdf_path) as doc:
        if strict_single_page and doc.page_count != 1: raise ValueError(f"not single slide {doc.page_count}: {pdf_path}")
        page = doc[0]
        scale = dpi / 72.0 
        mat = fitz.Matrix(scale, scale)
        pix = page.get_pixmap(matrix=mat, alpha=False)
    image_dir.mkdir(parents=True, exist_ok=True)
    fmt = fmt.lower()
    if fmt == "jpeg":
        fmt = "jpg"
    out_path = image_dir / f"{pdf_path.stem}.{fmt}"
    pix.save(out_path.as_posix())
    return out_path

### smaller the front size
def add_small_after_blocks(tex) -> str:
    text = tex
    pattern = re.compile(
        r'(?m)^([ \t]*)\\begin\{(?:block|alertblock|exampleblock)\}'
        r'(?:<[^>\n]*>)?(?:\[[^\]\n]*\])?\s*\{[^}]*\}[^\n]*\r?\n'
        r'([ \t]*)(?!\\small\b)'
    )
    def repl(m: re.Match) -> str:
        return f"{m.group(0)}\\footnotesize\n{m.group(2)}"
    new_text = pattern.sub(repl, text)
    return new_text

### smaller the figure size
def scale_includegraphics_widths(tex: str, factor: float, precision: int = 3, add_if_missing: bool = False) -> str:
    INCLUDE_RE = re.compile(
        r'\\includegraphics(?:\s*\[(?P<opts>[^\]]*)\])?\s*\{(?P<path>[^}]*)\}',
        re.DOTALL,
    )
    WIDTH_RE = re.compile(r'(?<![a-zA-Z])width\s*=\s*([^,\]]+)', re.IGNORECASE)
    REL_RE = re.compile(r'^\s*(?:(\d*\.?\d+)|\.(\d+))?\s*\\(textwidth|linewidth|columnwidth)\b')

    def scale_rel(expr: str) -> str | None:
        val = expr.strip().strip("{}")
        m = REL_RE.match(val)
        if not m:
            return None
        num = m.group(1)
        if num is None and m.group(2) is not None:
            num = "0." + m.group(2)
        k = 1.0 if not num else float(num)
        new_k = round(k * factor, precision)
        new_k_str = f"{new_k:g}"
        return f"{new_k_str}\\{m.group(3)}"

    def repl_inc(mm: re.Match) -> str:
        opts = mm.group("opts")
        path = mm.group("path")
        if opts is None or opts.strip() == "":
            if add_if_missing:
                return f"\\includegraphics[width={factor:g}\\textwidth]{{{path}}}"
            else:
                return mm.group(0)
        def repl_width(mw: re.Match) -> str:
            expr = mw.group(1)
            scaled = scale_rel(expr)
            return f"width={scaled}" if scaled is not None else mw.group(0)
        new_opts = WIDTH_RE.sub(repl_width, opts)
        if new_opts == opts and add_if_missing:
            new_opts = f"width={factor:g}\\textwidth," + opts.strip()
        return f"\\includegraphics[{new_opts}]{{{path}}}"
    return INCLUDE_RE.sub(repl_inc, tex)

def _line_starts(text):
    starts = [0]
    for m in re.finditer('\n', text):
        starts.append(m.end())
    return starts

def _pos_to_line(pos, line_starts):
    return bisect.bisect_right(line_starts, pos)

def compute_frame_spans(code: str):
    line_starts = _line_starts(code)
    sec_re  = re.compile(r'(?m)^\\section\*?(?:\[[^\]]*\])?\{([^}]*)\}')
    sub_re  = re.compile(r'(?m)^\\subsection\*?(?:\[[^\]]*\])?\{([^}]*)\}')

    sections = []
    for m in sec_re.finditer(code):
        pos = m.start()
        sections.append({
            "pos": pos,
            "line": _pos_to_line(pos, line_starts),
            "title": m.group(1).strip()
        })
    subsections = []
    for m in sub_re.finditer(code):
        pos = m.start()
        subsections.append({
            "pos": pos,
            "line": _pos_to_line(pos, line_starts),
            "title": m.group(1).strip()
        })

    sec_pos_list  = [s["pos"] for s in sections]
    sub_pos_list  = [s["pos"] for s in subsections]

    frame_re = re.compile(
        r'\\begin\{frame\}(?:<[^>\n]*>)?(?:\[[^\]\n]*\])?(?:\{.*?\}){0,2}.*?\\end\{frame\}',
        re.DOTALL
    )
    frametitle_re = re.compile(r'\\frametitle(?:<[^>]*>)?(?:\[[^\]]*\])?\{([^}]*)\}')

    frame_env_title_re = re.compile(
        r'^\\begin\{frame\}(?:<[^>\n]*>)?(?:\[[^\]\n]*\])?\s*\{([^}]*)\}',
        re.DOTALL
    )

    frames = []
    for i, m in enumerate(frame_re.finditer(code)):
        start, end = m.start(), m.end()
        start_line = _pos_to_line(start, line_starts)
        end_line   = _pos_to_line(end - 1, line_starts)
        text = m.group(0)

        t = frametitle_re.search(text)
        if t:
            title = t.group(1).strip()
        else:
            t2 = frame_env_title_re.search(text)
            title = t2.group(1).strip() if t2 else ""

        if sec_pos_list:
            j = bisect_right(sec_pos_list, start) - 1
            if j >= 0:
                sec_title = sections[j]["title"]
                sec_line  = sections[j]["line"]
            else:
                sec_title, sec_line = "", None
        else:
            sec_title, sec_line = "", None

        if sub_pos_list:
            k = bisect_right(sub_pos_list, start) - 1
            if k >= 0:
                sub_title = subsections[k]["title"]
                sub_line  = subsections[k]["line"]
            else:
                sub_title, sub_line = "", None
        else:
            sub_title, sub_line = "", None

        frames.append({
            "idx": i,
            "start": start,
            "end": end,
            "start_line": start_line,
            "end_line": end_line,
            "title": title,
            "section": sec_title,
            "section_line": sec_line,
            "subsection": sub_title,
            "subsection_line": sub_line,
            "text": text
        })

    return frames

## fix the grammer error with complie error
correct_prompt_path = "./prompts/slide_beamer_correct.txt"
def correcte_error(beamer_code, error_info, agent):
    with open(correct_prompt_path, 'r', encoding='utf-8') as f_prompt: templete_prompt = f_prompt.read()
    inference_prompt = (
        templete_prompt,
        "This is the latex code for slides:", beamer_code,
        "The errors are:", "\n".join(error_info)
    )
    inference_prompt = "\n".join(map(str, inference_prompt))
    print(len(inference_prompt))
    user_msg = BaseMessage.make_user_message(role_name="User", content=inference_prompt)
    response = safe_step(agent, user_msg)
    code = extract_beamer_code(response.msgs[-1].content)
    return code, response.info['usage']

def safe_step(agent, user_msg, max_retries=5):
    for attempt in range(max_retries):
        response = agent.step(user_msg)
        if getattr(response, "msgs", None) and len(response.msgs) > 0:
            return response
        print(f"[Retry {attempt+1}/{max_retries}] Empty or invalid response, retrying...")
    raise RuntimeError(f"Agent failed after {max_retries} retries: {user_msg}")

# def find_all_tex_files(root_dir):
#     tex_files = []
#     for dirpath, dirnames, filenames in os.walk(root_dir):
#         for filename in filenames:
#             if filename.endswith(".tex"):
#                 full_path = os.path.join(dirpath, filename)
#                 with open(full_path, 'r', encoding='utf-8') as f: 
#                     tex_files.append(f.read())
#     return tex_files
def find_all_tex_files(root_dir):
    tex_files = []
    for dirpath, dirnames, filenames in os.walk(root_dir):
        for filename in filenames:
            if filename.endswith(".tex"):
                full_path = os.path.join(dirpath, filename)
                try:
                    with open(full_path, 'r', encoding='utf-8') as f:
                        tex_files.append(f.read())
                except Exception as e:
                    print(f"⚠️ Skip {full_path}: {e}")
                    continue
    return tex_files

# tectonic 在 Windows 上会往 stderr 打两类与「编译成败」无关的噪声，必须先清掉：
#   1) ``Fontconfig error: Cannot load default config file: No such file: (null)``
#      —— 这行是 Windows 上找不到 fontconfig 配置的正常提示，但字面量里含
#      "error"。上游用 ``if "error" in feedback`` 子串判错，于是**每一次成功
#      编译都会被误判为失败**。
#   2) 每个被 xetex 探测到的系统字体各一条 ``warning: accessing absolute path ...``。
#      几十条这种警告会把真正需要关注的 ``Overfull`` 警告挤出
#      ``improve_layout`` 里 ``warning_info[:len//2]`` 的前半段，导致树搜索
#      以为「没有幻灯片需要改进」而静默什么都不做。
_BENIGN_FEEDBACK_PATTERNS = (
    re.compile(r"^Fontconfig error: .*$", re.MULTILINE),
    re.compile(r"^warning: accessing absolute path .*$", re.MULTILINE),
)


def _clean_feedback(text):
    """剔除与编译成败无关的噪声行，并去掉空行。"""
    for pattern in _BENIGN_FEEDBACK_PATTERNS:
        text = pattern.sub("", text)
    return "\n".join(line for line in text.splitlines() if line.strip())


def compile_tex(tex_path):
    """编译 tex，返回 ``(是否成功, 反馈文本)``。

    判定成败只认**进程退出码**，不再对输出做子串匹配 —— 子串匹配会被
    上面那行 Fontconfig 噪声骗到。反馈文本仍保留 stdout+stderr（已去噪），
    因为 ``improve_layout`` 需要从里面 grep ``^warning:`` 找 Overfull。
    """
    tex_path = Path(tex_path).resolve()
    if not tex_path.exists(): raise FileNotFoundError(f"Tex file {tex_path} does not exist")
    result = subprocess.run(
        ["tectonic", str(tex_path)],
        capture_output=True,
        text=True
    )
    ok = result.returncode == 0
    feedback = _clean_feedback("\n".join([result.stdout or "", result.stderr or ""]))
    print("Tex File Compilation succeeded." if ok else "Compilation failed:")
    print(feedback)
    return ok, feedback
