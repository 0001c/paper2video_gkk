"""结构化保真度检查：生成的 main.tex 是否忠实于原文。

为什么要专门查这个
------------------
LLM 排版最常出两类问题，而且**都"看着没问题"**：

1. **漏** —— 原文的某些章节/段落被吞掉。输出看起来完整，实则少了内容。
2. **编** —— 补造原文没有的数据、文献、图表。更危险，因为它让输出显得更"完整"。

单看生成的 tex 是判断不出来的，必须**双向比对**：
既查"原文有的、输出有没有"，也查"输出有的、原文有没有"。

匹配策略：以「前 20 字」作指纹做子串匹配，而不是整句相等 ——
两边切句方式不同（PDF 会被列宽切断），要求整句相等会大量假阴性。

用法：
    python scripts/check_struct_fidelity.py <原文> <main.tex>
"""
import argparse
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import paper_ingest  # noqa: E402


def tex_to_plain(tex):
    """把 LaTeX 源码近似还原成可读文本（去掉命令，但保留换行与标题文字）。"""
    text = re.sub(r"%.*$", "", tex, flags=re.M)                       # 注释
    text = re.sub(r"\\begin\{[^}]*\}(\[[^\]]*\])?", "\n", text)
    text = re.sub(r"\\end\{[^}]*\}", "\n", text)
    text = re.sub(r"\\[a-zA-Z@]+\*?", " ", text)                      # 命令名
    text = re.sub(r"\[[^\]]*\]", " ", text)                           # 可选参数
    text = re.sub(r"[{}~^_$]", " ", text)
    text = re.sub(r"[ \t]+", " ", text)
    return text


def _norm(text):
    """只保留汉字/字母/数字 —— 消除标点与空白差异。

    必须归一化的原因：原文用半角 `摘要:`、生成的 tex 用全角 `摘要：`；
    原文写 `Elisha&Ariel`、tex 里是 `Elisha \\& Ariel`。
    不归一化的话这些**全是假阴性**，报告会显示成"内容丢失"，
    而实际上一个字都没少 —— 这种假警报比不报还糟，会让人不再信任报告。
    """
    return re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]", "", text)


# 提取噪声：这些片段"丢失"是**预期行为**（本就该被丢掉），不该算作内容缺失
NOISE_MARKERS = ("评阅表", "学号", "姓名", "满分分数", "签字", "培养办公室",
                 "研究生院", "课程名称", "培养层次", "所在系", "完成日期",
                 "字数要求", "评分要求", "论文评语", "任课教师")


def _window_coverage(src_norm, out_norm, width=15, step=5):
    """按固定长度窗口算覆盖率，返回 (命中窗口数, 总窗口数)。

    为什么主指标用窗口而不是整句/前缀比对：
    两边切句方式不同（PDF 会被列宽切断），摘要前缀也可能不同
    （原文写「摘要:…」，生成的 tex 用 abstract 环境、没有这两个字）。
    这些都会让整句比对产生**假阴性** —— 报出一堆"丢失"而其实一个字没少。
    **假警报比不报更糟**：它会让人不再相信这份报告。
    固定窗口只看"这段局部内容出现过没有"，对切分方式不敏感。
    """
    if len(src_norm) < width:
        return (1 if src_norm and src_norm in out_norm else 0, 1)
    hit = total = 0
    for start in range(0, len(src_norm) - width + 1, step):
        window = src_norm[start:start + width]
        total += 1
        if window in out_norm:
            hit += 1
    return hit, total


def fingerprints(text, min_len=10, head=20):
    """把文本切成片段并取「前 N 字」的归一化指纹。"""
    parts = re.split(r"[。！？；!?;\n]+", text)
    out = []
    for part in parts:
        part = _norm(part)
        if len(part) >= min_len:
            out.append(part[:head])
    return out


def compare(source, tex_path, threshold=0.6):
    print("=" * 74)
    print("  结构化保真度检查")
    print("=" * 74)

    raw_text, meta = paper_ingest.ingest(source, os.path.join(
        os.path.dirname(os.path.abspath(tex_path)), "_fidelity_ingest"))
    with open(tex_path, encoding="utf-8") as handle:
        tex = handle.read()
    out_plain = tex_to_plain(tex)

    src_norm = _norm(raw_text)
    out_norm = _norm(out_plain)

    keep_hit, keep_total = _window_coverage(src_norm, out_norm)
    trace_hit, trace_total = _window_coverage(out_norm, src_norm)
    keep_rate = 100.0 * keep_hit / max(keep_total, 1)
    trace_rate = 100.0 * trace_hit / max(trace_total, 1)

    # 明细仍用片段级（粗略，仅供人工定位具体丢了什么）
    src_fp = fingerprints(raw_text)
    out_fp = fingerprints(out_plain)
    missing_raw = [f for f in src_fp if f not in out_norm]
    expected_drop = [f for f in missing_raw if any(k in f for k in NOISE_MARKERS)]
    missing = [f for f in missing_raw if f not in expected_drop]
    invented = [f for f in out_fp if f not in src_norm]

    print("  原文长度          : %d 字（归一化后）" % len(src_norm))
    print("  输出长度          : %d 字（归一化后）" % len(out_norm))
    print()
    print("  内容保留率        : %.1f%%  ← 原文有多少出现在输出里（越高越完整）" % keep_rate)
    print("  输出可溯源率      : %.1f%%  ← 输出有多少来自原文（越低越可能编造）" % trace_rate)
    print()
    print("  明细（片段级，仅供定位）:")
    print("      原文片段 %d / 输出片段 %d" % (len(src_fp), len(out_fp)))
    print("      预期噪声丢弃 %d（表单字段/页眉，本就该丢）｜疑似内容缺失 %d｜输出中待溯源 %d"
          % (len(expected_drop), len(missing), len(invented)))
    print()

    # 结构信号
    sections = re.findall(r"\\section\{([^}]*)\}", tex)
    subsections = re.findall(r"\\subsection\{([^}]*)\}", tex)
    print("  输出章节结构    : %d 个 \\section / %d 个 \\subsection"
          % (len(sections), len(subsections)))
    for name in sections:
        print("      § %s" % name)
    print()

    # LaTeX 层面的硬性约束
    checks = [
        ("含 \\usepackage{ctex}", "\\usepackage{ctex}" in tex),
        ("含 \\usepackage{tabularray}", "\\usepackage{tabularray}" in tex),
        ("未误用 tabular（会与 ctex 冲突）", not re.search(r"\\begin\{tabular\}", tex)),
        ("含 \\documentclass", tex.lstrip().startswith("\\documentclass")),
        ("无 markdown 代码块残留", "```" not in tex),
    ]
    print("  硬性约束:")
    for label, ok in checks:
        print("      %s %s" % ("OK " if ok else "!! ", label))
    print()

    print("  未命中窗口的常见**良性**原因（先排除这些再怀疑丢内容）：")
    print("      · 手写小节编号被移除（\\section 会自动编号）")
    print("      · 摘要前缀被 abstract 环境替代（原文「摘要:…」→ 环境本身）")
    print("      · 表单字段/页眉按预期丢弃")
    print("      · 模型对原文不一致处的主动规范化 —— 这条**属于偏差，需留意**：")
    print("        实测原文 `Elisha`/`Elishar` 混用（12:1），输出被统一成 `Elishar`。")
    print("        若原文的不一致是刻意的（如引用不同作者），这就改错了。")
    print()
    if missing:
        print("  --- 原文里丢失的片段（示例，前 5 条）---")
        for frag in missing[:5]:
            print("      ✗ %s..." % frag)
    if invented:
        print("  --- 输出里原文找不到的片段（示例，前 5 条）---")
        for frag in invented[:5]:
            print("      ? %s..." % frag)

    ok = (keep_rate >= 95 and trace_rate >= 90
          and not re.search(r"\\begin\{tabular\}", tex)
          and "\\usepackage{ctex}" in tex and "```" not in tex)
    print()
    print("  判定: %s  （门槛：保留率>=95%% 且可溯源率>=90%% 且 LaTeX 约束全过）"
          % ("通过" if ok else "需人工复核"))
    return 0 if ok else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description="检查 main.tex 相对原文的保真度")
    parser.add_argument("source", help="原文文件（.pdf/.docx/.doc）")
    parser.add_argument("tex", help="生成的 main.tex")
    args = parser.parse_args(argv)
    return compare(args.source, args.tex)


if __name__ == "__main__":
    sys.exit(main())
