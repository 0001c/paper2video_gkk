"""论文正文 → main.tex：让 LLM 把提取出的文本重组成可编译的 LaTeX 源码。

为什么单独一层
--------------
提取（`paper_ingest`）是**纯本地、零成本**的，可以随便调、反复测；
结构化要调 LLM、**花钱**、还慢。拆开之后两层的质量可以分开评估 ——
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
import io
import json
import os
import re
import subprocess
import sys
import time

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
  去掉，以及移除正文里手写的小节编号（`\section` 会自动编号）。
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


def call_llm(system_prompt, user_prompt, model=None, max_tokens=32768,
             temperature=0.2, timeout=900):
    """调用 OpenAI 兼容接口。返回 (正文, usage)。

    为什么直接用 openai SDK 而不是项目里的 camel：
    这一步是**独立于流水线**的离线工具，不需要 camel 的 agent 封装；
    直接用 SDK 更少黑盒、出错位置更明确。环境变量由 pv_env 统一准备。
    """
    sys.path.insert(0, PAPER2VIDEO_SRC)
    import pv_env  # noqa: F401  触发 .env 加载与别名同步
    from openai import OpenAI

    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    base_url = os.environ.get("OPENAI_API_BASE", "").strip()
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY 未配置（应在仓库根的 .env 里）")

    client = OpenAI(api_key=api_key, base_url=base_url or None, timeout=timeout)
    model = model or DEFAULT_MODEL

    started = time.time()
    resp = client.chat.completions.create(
        model=model,
        messages=[{"role": "system", "content": system_prompt},
                  {"role": "user", "content": user_prompt}],
        max_tokens=max_tokens,
        temperature=temperature,
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
    workdir = os.path.dirname(os.path.abspath(tex_path)) or "."
    try:
        result = subprocess.run(["tectonic", os.path.basename(tex_path)],
                                cwd=workdir, capture_output=True, text=True,
                                timeout=timeout)
    except FileNotFoundError:
        return False, "tectonic 未找到（应装在 E:\\ai-models\\bin）"
    except subprocess.TimeoutExpired:
        return False, "tectonic 编译超时（%ds）" % timeout
    log = (result.stdout or "") + "\n" + (result.stderr or "")
    return result.returncode == 0, log


def structurize(source, out_dir, model=None, max_tokens=32768,
                do_compile=True, keep_text=False):
    """完整流程：提取 → 结构化 → 落盘 → （可选）编译验证。"""
    os.makedirs(out_dir, exist_ok=True)

    print("[1/3] 提取正文 ...")
    text, meta = paper_ingest.ingest(source, os.path.join(out_dir, "ingest"))
    if not text:
        raise RuntimeError("提取结果为空 —— 若来源是 LaTeX（.tex/.zip），"
                           "本就无需结构化（meta['needs_llm']=False）")
    print("      格式=%s 字符=%d" % (meta.get("detected_format"), len(text)))

    print("[2/3] 调用 %s 结构化 ..." % (model or DEFAULT_MODEL))
    user_prompt = USER_TEMPLATE.format(
        fmt=meta.get("detected_format", "?"),
        chars=len(text),
        quality_hint=_quality_hint(meta),
        text=text)
    content, info = call_llm(SYSTEM_PROMPT, user_prompt, model=model,
                             max_tokens=max_tokens)
    tex = strip_code_fence(content)
    print("      %.1fs | tokens=%s | finish=%s"
          % (info["elapsed"], info["usage"].get("total_tokens"),
             info["finish_reason"]))

    if info["finish_reason"] == "length":
        print("      ⚠️ finish_reason=length —— 输出被 max_tokens 截断，"
              "LaTeX 很可能不完整（可提高 --max-tokens 或分段）")

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
    parser.add_argument("--max-tokens", type=int, default=32768)
    parser.add_argument("--no-compile", action="store_true", help="跳过 tectonic 编译")
    args = parser.parse_args(argv)

    tex, report = structurize(args.source, args.out, model=args.model,
                              max_tokens=args.max_tokens,
                              do_compile=not args.no_compile)
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
