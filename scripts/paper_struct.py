"""正文 → main.tex：PDF/审核正文在本地保真排版，Word 可选模型排版。

为什么单独一层
--------------
提取（`paper_ingest`）是**纯本地、零成本**的，可以随便调、反复测；
结构化中的可选模型调用会产生费用。拆开之后两层的质量可以分开评估 ——
出问题时能立刻判断是"提取没提对"还是"模型没排对"，而不是一起返工。

⚠️ 写进 prompt 的四条硬约束（都是本项目的实测结论）
----------------------------------------------------
1. **中文必须 `\\usepackage{ctex}`**，且**表格必须用 `tabularray` 的 `tblr`** ——
   ctex 与传统 `tabular`/`booktabs` 存在**宏包级冲突**，只要同时出现就必然报
   `Misplaced \\noalign`，与单元格是否含中文无关（纯英文表格同样失败）。
2. **真实论文的标题没有样式标记**：实测使用者手写的 Word 文档 heading 数为 0，
   标题与正文混在一起。必须让模型按内容语义与数字序号识别章节。
3. **不得编造**：源文本没有的数据、数据集名、指标数值、参考文献条目一律不写。
4. **只输出 LaTeX 源码**，不要 markdown 代码块包裹 —— 虽然这里也做了兜底剥离。

用法
----
    python scripts/paper_struct.py <输入文件> [--out 目录] [--no-compile]
"""
import argparse
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import paper_ingest  # noqa: E402

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAPER2VIDEO_SRC = os.path.join(REPO_ROOT, "Paper2Video-main", "src")

DEFAULT_MODEL = os.environ.get("PAPER2VIDEO_LLM_MODEL", "deepseek-flash")


SYSTEM_PROMPT = """你是学术论文的 LaTeX 排版专家。用户会给你一篇论文的正文纯文本，\
你的任务是基于**且仅基于**这份文本，产出一份可直接用 XeLaTeX 编译的 LaTeX 源码。

输出格式（严格遵守）：
- 只输出 LaTeX 源码本身。不要 markdown 代码块标记（不要 ```），不要任何解释、
  前言、后语、注释性说明。
- 第一行就是 \\documentclass。

导言区要求：
- \\documentclass[11pt]{article}
- \\usepackage{ctex}        ← 必需，否则中文无法编译
- \\usepackage{tabularray}  ← 表格必需（见下）
- \\usepackage{graphicx}    ← 即使没有图也保留
- \\usepackage{amsmath}、\\usepackage{geometry} 可按需加

表格的硬性约束（违反必然编译失败）：
- **禁止使用 \\begin{tabular}**。传统 tabular / booktabs 与 ctex 宏包存在
  宏包级冲突，只要同时出现就必然报 "Misplaced \\noalign"，与单元格是否含中文无关。
- 所有表格一律写成：
  \\begin{tblr}{colspec={lcc}, hlines, row{1}={font=\\bfseries}}
  ... & ... \\\\
  \\end{tblr}
- 如果原文没有表格，就不要生成表格。

章节结构：
- 用 \\section / \\subsection 组织，保留原文的章节顺序与标题含义。
- ⚠️ 提取出的文本**没有任何标题样式标记**，标题和正文是混在一起的，
  甚至可能缺少编号。请按这些线索识别章节标题：
  · 中文序号：一、二、三 / 第一章 / 第 2 节 / （三）
  · 阿拉伯序号：1 / 1.1 / 2.3
  · 固定章节名：摘要、关键词、引言、绪论、文献综述、研究方法、分析与讨论、
    结论、结语、参考文献、致谢、附录
- 摘要用 \\begin{abstract}...\\end{abstract}；关键词单独一段。
- 标题页用 \\title{} + \\author{} + \\maketitle。

内容的硬性约束：
- **绝对不要编造**任何源文本中没有的内容：不添加数据集名、指标数值、实验结果、
  参考文献条目、图表编号。宁可少写，也不要补造。
- **忠实于原文措辞与术语，不要"顺手纠正"**：实测你容易把原文前后不一致的地方
  （例如人名拼写 `Elisha` / `Elishar` 混用）统一成你认为正确的形式。
  即使确实是笔误，**也请原样保留** —— 你的职责是排版，不是校对。
  唯一允许的改写是把明显的提取噪声（页眉页脚、"课程论文评阅表"之类的表单字段）
  去掉，以及移除正文里手写的小节编号（`\\section` 会自动编号）。
- 参考文献：只把源文本里已有的条目原样列出（可放入 thebibliography 环境）；
  原文没有就省略整个环境，不要自己编造引用。
- 源文本里若混入明显的提取噪声（页眉页脚、"课程论文评阅表"之类的表单字段、
  学号、签字栏），请**忽略它们**，不要写进 LaTeX。

输出前自检（逐条确认后再输出）：
1. 含 \\usepackage{ctex} 吗？
2. 有没有误用 \\begin{tabular}？
3. 有没有写入源文本中不存在的数据或文献？
4. 有没有"顺手纠正"原文的用词、拼写或数字？有的话改回原样。
5. 输出里有没有 markdown 代码块标记？"""


USER_TEMPLATE = """下面是这篇论文的正文文本（由 {fmt} 提取，共 {chars} 字符）。

{quality_hint}

请据此生成 LaTeX 源码。记住：只输出 LaTeX 源码本身。

──────────── 正文开始 ────────────
{text}
──────────── 正文结束 ────────────"""


def _quality_hint(meta):
    """把提取层的已知缺陷提前告诉模型，让它主动防一手。"""
    hints = []
    if meta.get("lossy"):
        hints.append("- 该文件是**有损提取**（老版 .doc）：没有标题层级，"
                     "且可能混入页眉页脚或表单字段。")
    if meta.get("format") == "pdf":
        hints.append("- PDF 提取的文本里，一行可能被列宽截断，"
                     "跨行的句子需要你自行接续理解。")
    if meta.get("headings") == 0:
        hints.append("- 原文未使用 Word 标题样式，**标题没有任何标记**，"
                     "请完全依靠内容语义与序号来划分章节。")
    if not hints:
        return ""
    return "提取过程中已知的情况（请在排版时注意）：\n" + "\n".join(hints)


def strip_code_fence(text):
    """剥离 markdown 代码块包裹。

    实测模型即使被明确要求"不要代码块"也偶尔会加，而且形式不统一：
    有的是整段被包住，有的是先来一句"好的，以下是 LaTeX 源码："再跟代码块。
    所以要做两级匹配 + 一次兜底，只处理最规整的那种是不够的。
    """
    text = (text or "").strip()

    # ① 整段被代码块包裹
    fence = re.match(r"^```[A-Za-z]*\s*\n(.*?)\n```\s*$", text, re.S)
    if fence:
        text = fence.group(1).strip()
    else:
        # ② 前置解释文字 + 代码块：把代码块内部取出来
        inner = re.search(r"```[A-Za-z]*\s*\n(.*?)\n```", text, re.S)
        if inner and "\\documentclass" in inner.group(1):
            text = inner.group(1).strip()

    # ③ 从 \documentclass 开始截取，去掉残留的解释性开头
    idx = text.find("\\documentclass")
    if idx > 0:
        text = text[idx:]

    # ④ 删掉任何以 ``` 开头的行（孤立的三反引号）。LaTeX 里不会出现这种行，
    #    所以这样删是安全的；比"从第一个 ``` 截断到结尾"更保守。
    text = re.sub(r"^[ \t]*```.*$", "", text, flags=re.M)
    return text.strip()


def call_llm(system_prompt, user_prompt, model=None, max_tokens=None,
             temperature=0.2, timeout=900, model_snapshot=None):
    """调用 OpenAI 兼容接口。返回 (正文, usage)。

    为什么直接用 openai SDK 而不是项目里的 camel：
    这一步是**独立于流水线**的离线工具，不需要 camel 的 agent 封装；
    直接用 SDK 更少黑盒、出错位置更明确。环境变量由 pv_env 统一准备。
    """
    sys.path.insert(0, PAPER2VIDEO_SRC)
    import pv_env  # noqa: F401  触发 .env 加载与别名同步
    from openai import OpenAI
    from model_runtime import role_settings

    settings = role_settings(model_snapshot, 'text')
    if settings:
        if (model and model != settings['model_type']) or max_tokens is not None:
            raise ValueError('v2结构化使用任务文字参数，不接受独立型号/输出上限覆盖')
        model = settings['model_type']
        api_key = os.environ[settings['api_key_env']]
        base_url = settings['url']
        parameters = {'temperature': temperature, **settings['model_config']}
    else:
        api_key = os.environ.get("OPENAI_API_KEY", "").strip()
        base_url = os.environ.get("OPENAI_API_BASE", "").strip()
        parameters = {'temperature': temperature}
    if not api_key:
        raise RuntimeError("文字模型凭据未配置")

    model = model or DEFAULT_MODEL
    # 未知服务商模型由其默认输出上限决定，不强塞 DeepSeek 的 65536 上限。
    if not settings and max_tokens is None and model in ('deepseek-flash', 'deepseek-v4-flash', 'deepseek-chat', 'deepseek-reasoner'):
        max_tokens = int(os.environ.get('PAPER2VIDEO_MAX_TOKENS') or '65536')

    started = time.time()
    from web import model_costs
    ticket = model_costs.begin('text', model, base_url) if settings and settings.get('provider') == 'bailian' else None
    with OpenAI(api_key=api_key, base_url=base_url or None, timeout=timeout,
                **({'max_retries': 0} if settings and settings.get('provider') == 'bailian' else {})) as client:
        resp = client.chat.completions.create(
            model=model,
            messages=[{"role": "system", "content": system_prompt},
                      {"role": "user", "content": user_prompt}],
            **({'max_tokens': max_tokens} if max_tokens is not None else {}),
            **parameters,
        )
    elapsed = time.time() - started

    choice = resp.choices[0]
    content = choice.message.content or ""
    usage = {}
    if getattr(resp, "usage", None):
        usage = {
            "prompt_tokens": resp.usage.prompt_tokens,
            "completion_tokens": resp.usage.completion_tokens,
            "total_tokens": resp.usage.total_tokens,
        }
    model_costs.finish(ticket, usage)

    # 推理模型：思维链可能单独放在 reasoning_content 里，不能当成正文
    reasoning = getattr(choice.message, "reasoning_content", None)
    if reasoning:
        usage["reasoning_chars"] = len(reasoning)

    finish = getattr(choice, "finish_reason", None)
    return content, {"usage": usage, "elapsed": elapsed, "finish_reason": finish,
                     "model": model}


def compile_tex(tex_path, timeout=600):
    """用 tectonic 编译。返回 (是否成功, 日志)。

    ⚠️ 必须按**退出码**判断成败：tectonic 在 Windows 上每次编译（含成功）都会
    往 stderr 打 `Fontconfig error: Cannot load default config file` 之类的噪声，
    按子串判错会把每一次成功都误判成失败。
    """
    # 本地原文排版不经过 call_llm，也需要项目配置的工具 PATH。
    sys.path.insert(0, PAPER2VIDEO_SRC)
    import pv_env  # noqa: F401
    workdir = os.path.dirname(os.path.abspath(tex_path)) or "."
    try:
        # 必须显式指定编码：不给 encoding 时 subprocess 按**平台默认编码**解码
        # （Windows 上是 GBK），而 tectonic 输出的是 UTF-8 —— 实测会抛
        # UnicodeDecodeError: 'gbk' codec can't decode byte 0x93，
        # 且异常发生在 reader 线程里，表现为"编译失败"但日志残缺，极易误判。
        result = subprocess.run(["tectonic", os.path.basename(tex_path)],
                                cwd=workdir, capture_output=True, text=True,
                                encoding="utf-8", errors="replace",
                                timeout=timeout)
    except FileNotFoundError:
        return False, "tectonic 未找到（应装在 E:\\ai-models\\bin）"
    except subprocess.TimeoutExpired:
        return False, "tectonic 编译超时（%ds）" % timeout
    log = (result.stdout or "") + "\n" + (result.stderr or "")
    missing = [line for line in log.splitlines() if 'could not represent character' in line and '"nullfont"' not in line]
    if missing:
        log += '\nerror: 正文字体缺少实际使用的字形：\n' + '\n'.join(missing)
    return result.returncode == 0 and not missing, log


CONTINUE_TEMPLATE = """下面是一份 LaTeX 源码的**末尾片段**。它因为长度上限被截断了，还没写完。

请**接着它继续输出剩余的 LaTeX 内容**：
- 从断点处直接续写，**不要重复**任何已经出现过的内容；
- **不要**重新输出 \\documentclass 或任何 \\usepackage（前言已经写过了）；
- 若断点落在一张表 / 列表 / 环境的中间，先把那一处补完整，再继续往下写；
- 不要输出解释文字，也不要加 markdown 代码块；
- 一直写到 \\end{{document}} 为止。

──────── 已输出内容的末尾片段（供你判断断点）────────
{tail}
──────── 片段结束 ────────"""


def _tex_is_complete(tex):
    return "\\end{document}" in tex


def _repair_unclosed(tex):
    """把被截断的 LaTeX 收尾成**可编译**的样子，返回 (源码, 是否真的动过)。

    做法：从左到右扫 begin/end 配对，找出"所有内层环境都闭好了"的最后一个位置，
    截到那里，再补上 ``\\end{document}``。

    ⚠️ 判据不是"环境栈为空"——正文整体包在 ``\\begin{document}`` 里，
    文档级扫描下栈**永远不会空**（第一版就是这么写错的：65116 字符被削成 17 字符）。
    正确的基准是 ``\\begin{document}`` 之后的深度，栈回到该深度即为一个安全切点。

    为什么值得做：截断点常常落在一张表或一个列表中间，此时 LaTeX 会报
    ``File ended while scanning use of \\environment tblr`` —— 编译直接失败；
    而这份 main.tex 还要作为**下游生成幻灯片的输入**，把半个环境喂给模型，
    只会让那部分幻灯片变得不可控。丢掉尾部那一小段未完成内容，比留一个坏文件好。
    """
    token = re.compile(r"\\(begin|end)\{([^}]*)\}")
    stack = []
    baseline = None
    last_balanced = None
    for match in token.finditer(tex):
        kind, name = match.group(1), match.group(2)
        if kind == "begin":
            stack.append(name)
            if name == "document" and baseline is None:
                baseline = len(stack)          # 正文的基准深度
        elif stack and stack[-1] == name:
            stack.pop()
        if baseline is not None and len(stack) == baseline:
            last_balanced = match.end()

    if baseline is None or last_balanced is None:
        return tex, False                      # 连 \begin{document} 都没有，不做猜测
    if not stack:
        return tex, False                      # 栈回到 0 = 已见到 \end{document}，本就完整
    head = tex[:last_balanced].rstrip()
    dropped = len(tex) - len(head)
    if dropped > 0.3 * len(tex):
        print("      ⚠️ 收尾会丢弃 %.0f%% 的内容（截断位置很靠前）—— "
              "建议提高 PAPER2VIDEO_MAX_TOKENS 后重跑结构化"
              % (100.0 * dropped / len(tex)))
    return head + "\n\n\\end{document}\n", True


def _continue_tex(tex, info, model, max_tokens, max_rounds=3, model_snapshot=None):
    """撞上限后**续写**，尽量把整篇拿全。

    实测触发条件：24 页 / 6.6 万字符的论文，思维链就吃掉一大半额度
    （那次 reasoning 34722 字符），正文写到一张表中间就没有额度了。
    提高上限能缓解（接口实测接受 ≥131072），但论文再长还是会撞 ——
    所以这里做成"撞了就接着写"，而不是只把上限调大。
    """
    rounds = 0
    total = dict(info.get("usage") or {})
    elapsed = info.get("elapsed", 0.0)
    while not _tex_is_complete(tex) and rounds < max_rounds:
        rounds += 1
        tail = tex[-2500:]
        piece, more = call_llm(SYSTEM_PROMPT, CONTINUE_TEMPLATE.format(tail=tail),
                               model=model, max_tokens=max_tokens, model_snapshot=model_snapshot)
        piece = strip_code_fence(piece)
        print("      续写第 %d 轮：%.1fs | finish=%s | +%d 字符"
              % (rounds, more["elapsed"], more["finish_reason"], len(piece)))
        if not piece.strip():
            break
        tex = tex.rstrip() + "\n" + piece
        for key, value in (more.get("usage") or {}).items():
            if isinstance(value, (int, float)):
                total[key] = total.get(key, 0) + value
        elapsed += more["elapsed"]
        if more["finish_reason"] != "length":
            break

    info = dict(info)
    info["usage"] = total
    info["elapsed"] = elapsed
    info["continuation_rounds"] = rounds

    if not _tex_is_complete(tex):
        tex, repaired = _repair_unclosed(tex)
        info["repaired"] = repaired
        print("      ⚠️ 仍未见 \\end{document}：已截到最后一个环境配平点并补收尾"
              "（尾部未完成的一小段内容会丢失，但产物保证可编译）")
    return tex, info


def verified_source_binding(tex_path, source=None, text_path=None):
    try:
        target = Path(tex_path)
        binding = json.loads(target.with_name('source_binding.json').read_text(encoding='utf-8'))
        if (binding.get('version') != 1 or binding.get('compile_ok') is False
                or binding.get('tex_sha256') != hashlib.sha256(target.read_bytes()).hexdigest()):
            return False
        if source:
            if not Path(source).is_file() or binding.get('source_file_sha256') != hashlib.sha256(Path(source).read_bytes()).hexdigest():
                return False
        if text_path and Path(text_path).is_file() and binding.get('source_sha256') != hashlib.sha256(Path(text_path).read_text(encoding='utf-8').encode()).hexdigest():
            return False
        for name, digest in binding.get('glyph_images', {}).items():
            if not re.fullmatch(r'figures/source_p\d{4}_\d{2}\.png', name):
                return False
            image = target.parent / name
            if not image.is_file() or hashlib.sha256(image.read_bytes()).hexdigest() != digest:
                return False
        return True
    except (OSError, ValueError, AttributeError):
        return False


def structurize(source, out_dir, model=None, max_tokens=None,
                do_compile=True, keep_text=False, text_file=None, model_snapshot=None):
    """完整流程：提取 → 结构化 → 落盘 → （可选）编译验证。"""
    os.makedirs(out_dir, exist_ok=True)

    print("[1/3] 提取正文 ...")
    if text_file:
        with io.open(text_file, encoding="utf-8") as handle:
            text = handle.read()
        meta = {"detected_format": "selected course source", "chars": len(text)}
    else:
        text, meta = paper_ingest.ingest(source, os.path.join(out_dir, "ingest"))
    if not text:
        raise RuntimeError("提取结果为空 —— 若来源是 LaTeX（.tex/.zip），"
                           "本就无需结构化（meta['needs_llm']=False）")
    print("      格式=%s 字符=%d" % (meta.get("detected_format"), len(text)))
    if paper_ingest.has_unmapped_glyphs(text):
        if not meta.get('review_pages'):
            raise RuntimeError('选定正文仍含未识别字形，请对照源页修正；已在收费结构化前停止')
    if meta.get('review_pages'):
        raise RuntimeError('源文第 {} 页存在未识别字形/扫描内容，已保存高分辨率 source_pages 证据；'
                           '请对照源页修正正文，再通过 --text-file 使用审核文本。已在收费结构化前停止，不猜公式。'
                           .format('、'.join(map(str, meta['review_pages']))))

    source_only = meta.get('format') == 'pdf' or bool(text_file)
    glyph_names = set(paper_ingest.GLYPH_IMAGE_RE.findall(text))
    glyph_images = meta.get('glyph_images', [])
    if glyph_names and not glyph_images:
        if paper_ingest.sniff_format(source) != 'pdf':
            raise RuntimeError('原文字形图缺少对应 PDF 来源，请重新提取材料')
        _, pdf_meta = paper_ingest.extract_pdf(source)
        glyph_images = pdf_meta['glyph_images']
    glyph_images = [image for image in glyph_images if image['name'] in glyph_names]
    if glyph_names != {image['name'] for image in glyph_images}:
        raise RuntimeError('原文字形图与源 PDF 不一致，请重新提取材料')
    tex_dir = os.path.join(out_dir, 'latex_proj')
    if glyph_images:
        paper_ingest.save_glyph_images(source, glyph_images, os.path.join(tex_dir, 'figures'))
    print('[2/3] 本地原文排版 ...' if source_only else '[2/3] 调用 {} 结构化 ...'.format(model or DEFAULT_MODEL))
    user_prompt = USER_TEMPLATE.format(
        fmt=meta.get("detected_format", "?"),
        chars=len(text),
        quality_hint=_quality_hint(meta),
        text=text)
    system_prompt = SYSTEM_PROMPT + ("\n本次材料是已选择的教学内容。保留教材或课程的原章节、公式、例题和推导，不套用论文研究结构。" if text_file else "")
    if source_only:
        content, info = '', {'model': 'local-source-layout', 'usage': {}, 'elapsed': 0,
                            'finish_reason': 'stop'}
    else:
        content, info = call_llm(system_prompt, user_prompt, model=model,
                                 max_tokens=max_tokens, model_snapshot=model_snapshot)
    tex = strip_code_fence(content)
    print("      %.1fs | tokens=%s | finish=%s"
          % (info["elapsed"], info["usage"].get("total_tokens"),
             info["finish_reason"]))

    if info["finish_reason"] == "length":
        # 输出撞上限 —— 长论文（实测 24 页 / 6.6 万字符）一次装不下。
        # 直接收下会得到一个"少了后半篇"的 main.tex，而它还要喂给下游生成幻灯片，
        # 所以先尝试续写；实在拿不全再退化成"截到最后一个配平点 + 补 \end{document}"，
        # 保证产物至少是**可编译**的。
        print("      ⚠️ finish_reason=length —— 输出撞上限，开始续写 ...")
        tex, info = _continue_tex(tex, info, model, max_tokens, model_snapshot=model_snapshot)

    # 模型只负责版式；任何文本偏差都改用本地原文排版，不能靠编译成功放行。
    from check_struct_fidelity import tex_to_plain
    def norm(value):
        return ''.join(char for char in value if char.isalnum() or char in '=+-*/<>^∂∇≤≥×÷')
    paragraphs = [part.strip() for part in re.split(r'\n\s*\n', text) if part.strip()]
    plain = norm(tex_to_plain(tex))
    missing = [index for index, paragraph in enumerate(paragraphs) if norm(paragraph) not in plain]
    source_plain = norm(text)
    untraced = [part for part in re.split(r'[。！？!?\n]+', tex_to_plain(tex))
               if len(norm(part)) >= 15 and norm(part) not in source_plain]
    fallback = bool(source_only or missing or untraced or info.get('repaired'))
    if fallback:
        candidate = os.path.join(out_dir, 'struct_candidate.tex')
        if tex:
            with io.open(candidate, 'w', encoding='utf-8') as handle:
                handle.write(tex)
        escapes = {'\\': r'\textbackslash{}', '&': r'\&', '%': r'\%', '$': r'\$', '#': r'\#',
                   '_': r'\_', '{': r'\{', '}': r'\}', '~': r'\textasciitilde{}', '^': r'\textasciicircum{}'}
        def source_paragraph(paragraph):
            parts, end = [], 0
            for match in paper_ingest.GLYPH_IMAGE_RE.finditer(paragraph):
                parts.append(''.join(escapes.get(c, c) for c in paragraph[end:match.start()]))
                page = next(image['page'] for image in glyph_images if image['name'] == match[1])
                parts.append('\n% source-page: {}\n\\par\\noindent\\includegraphics[width=\\linewidth,keepaspectratio]{{figures/{}}}\\par\n'.format(page, match[1]))
                end = match.end()
            parts.append(''.join(escapes.get(c, c) for c in paragraph[end:]))
            return ''.join(parts)
        body = '\n\n'.join('% source-paragraph: {} sha256={}\n{}'.format(
            index, hashlib.sha256(paragraph.encode()).hexdigest(), source_paragraph(paragraph))
            for index, paragraph in enumerate(paragraphs))
        tex = '\\documentclass[11pt]{article}\n\\usepackage{ctex}\n\\usepackage{tabularray}\n\\usepackage{graphicx}\n\\begin{document}\n' + body + '\n\\end{document}'
        print('      [保真门禁] 采用本地原文排版，保留全部原句、数字和章节顺序')

    tex_dir = os.path.join(out_dir, "latex_proj")
    os.makedirs(tex_dir, exist_ok=True)
    tex_path = os.path.join(tex_dir, "main.tex")
    with io.open(tex_path, "w", encoding="utf-8", newline="") as handle:
        handle.write(tex)

    report = {
        "source": os.path.abspath(source),
        "detected_format": meta.get("detected_format"),
        "source_chars": len(text),
        "tex_path": os.path.abspath(tex_path),
        "tex_chars": len(tex),
        "sections": len(re.findall(r"\\section\{", tex)),
        "subsections": len(re.findall(r"\\subsection\{", tex)),
        "has_ctex": "\\usepackage{ctex}" in tex,
        "uses_tabular": bool(re.search(r"\\begin\{tabular\}", tex)),
        "uses_tblr": bool(re.search(r"\\begin\{tblr\}", tex)),
        "model": info["model"],
        "usage": info["usage"],
        "elapsed": round(info["elapsed"], 1),
        "finish_reason": info["finish_reason"],
        # 截断相关：续写了几轮、以及是否退化成"截到配平点 + 补收尾"
        "continuation_rounds": info.get("continuation_rounds", 0),
        "repaired": info.get("repaired", False),
        "fidelity": {'version': 1, 'source_sha256': hashlib.sha256(text.encode()).hexdigest(),
                     'literal_fallback': fallback, 'missing_paragraphs': missing,
                     'untraced_fragments': len(untraced), 'paragraph_count': len(paragraphs)},
    }

    if do_compile:
        print("[3/3] tectonic 编译验证 ...")
        ok, log = compile_tex(tex_path)
        report["compile_ok"] = ok
        tail = [ln for ln in log.splitlines() if ln.strip()][-12:]
        report["compile_log_tail"] = tail
        print("      编译%s" % ("成功" if ok else "失败"))
        if not ok:
            for line in tail[-6:]:
                print("      |", line[:150])

    with io.open(os.path.join(out_dir, "struct_report.json"), "w",
                 encoding="utf-8", newline="") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)
    with io.open(os.path.join(tex_dir, 'source_binding.json'), 'w', encoding='utf-8') as handle:
        json.dump({'version': 1, 'tex_sha256': hashlib.sha256(tex.encode()).hexdigest(),
                   'compile_ok': report.get('compile_ok'),
                   'source_file_sha256': hashlib.sha256(Path(source).read_bytes()).hexdigest() if Path(source).is_file() else None,
                   'source_sha256': report['fidelity']['source_sha256'], 'literal_fallback': fallback,
                   'glyph_images': {'figures/' + image['name']: hashlib.sha256(Path(tex_dir, 'figures', image['name']).read_bytes()).hexdigest() for image in glyph_images},
                   'paragraphs': [{'index': index, 'text': paragraph, 'sha256': hashlib.sha256(paragraph.encode()).hexdigest()}
                                  for index, paragraph in enumerate(paragraphs)]}, handle, ensure_ascii=False, indent=2)
    if do_compile and not report.get('compile_ok'):
        raise RuntimeError('保真结构化产物编译失败，已保存报告；不会进入课件生成')

    if not keep_text:
        pass
    return tex, report


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="论文正文 → main.tex（提取 + LLM 结构化 + 编译验证）")
    parser.add_argument("source", help="论文文件（.pdf/.docx/.doc）")
    parser.add_argument("--out", default=os.path.join(REPO_ROOT, ".workbuddy",
                                                      "struct_out"),
                        help="输出目录")
    parser.add_argument("--model", default=None, help="模型名（默认 deepseek-flash）")
    parser.add_argument('--model-config', help='无秘密的v2任务快照；结构化与续写使用文字角色')
    parser.add_argument("--max-tokens", type=int, default=None, help='覆盖模型默认输出上限；DeepSeek 默认 65536')
    parser.add_argument("--no-compile", action="store_true", help="跳过 tectonic 编译")
    parser.add_argument("--text-file", help="使用已选定的课程正文（UTF-8），不重新提取整个源文件")
    args = parser.parse_args(argv)

    model_snapshot = None
    if args.model_config:
        sys.path.insert(0, PAPER2VIDEO_SRC)
        from model_runtime import load_model_snapshot, role_settings
        model_snapshot, _ = load_model_snapshot(args.model_config)
        os.environ['PAPER2VIDEO_MODEL_CONFIG_ORIGIN'] = model_snapshot['model_config_origin']
        if model_snapshot['model_config_origin'] == 'v2':
            for name in list(os.environ):
                if name.startswith(('OPENAI_', 'DEEPSEEK_')):
                    os.environ.pop(name)
        settings = role_settings(model_snapshot, 'text')
        if settings:
            args.model = settings['model_type']

    tex, report = structurize(args.source, args.out, model=args.model,
                              max_tokens=args.max_tokens,
                              do_compile=not args.no_compile, text_file=args.text_file, model_snapshot=model_snapshot)
    print()
    print("=== 结果 ===")
    for key in ("tex_path", "tex_chars", "sections", "subsections", "has_ctex",
                "uses_tabular", "uses_tblr", "usage", "elapsed",
                "compile_ok", "finish_reason"):
        if key in report:
            print("   %-14s %s" % (key, report[key]))
    print()
    print("=== main.tex 开头 900 字符 ===")
    print(tex[:900])
    return 0


if __name__ == "__main__":
    sys.exit(main())
