"""输出语言的档位 —— 把「输出什么语言」收敛成**两个独立的轴**。

为什么是两个轴，而不是一个开关
------------------------------
「幻灯片语言」与「讲稿语言」是互相独立的，而且组合起来才是真实需求：

| 幻灯片 | 讲稿 | 场景 |
|---|---|---|
| en | en | 国际会议 / 投稿（原项目默认） |
| **en** | **zh** | **国内组会、中文答辩 —— 手里是英文 paper 的英文 slides，但要用中文讲** |
| zh | zh | 国内汇报 / 科普（幻灯片侧尚未实现，见 `resolve` 的报错） |

第二行是国内**最常见**的中文场景，而单个 `--video_lang` 开关会恰好把它排除掉
（因为它既不是"全英"也不是"全中"）。所以本模块区分两个轴，另提供组合预设。

输入语言不需要检测
------------------
**不要**试图检测论文原文的语言：幻灯片是 LLM **重写**的、讲稿是 VLM 看
**幻灯片图**写的，输入是什么语言都不影响输出。只要控制 prompt 里要求的
输出语言就够了。

⚠️ 反直觉但重要的约束：**光标描述必须始终是英文**
--------------------------------------------------
讲稿文件的格式是 ``<讲稿句子> | <cursor description>``。竖线右侧**不是内容**，
而是喂给 GUI-Owl 的 grounding 指令 —— 见 `cursor_gen.build_grounding_prompt`，
那段 system prompt 是从上游照搬的**英文工具调用协议**（代码注释明确写
"不能自创提示词"）。它最终只变成 `cursor.json` 里的一个坐标 `[x, y]`，
**不会出现在成片里**（成片的字幕取的是 `text`/`raw_text`，即竖线左侧）。

所以中文讲稿配英文光标描述是最优解：讲稿 / TTS / 字幕是中文，
而定位模型拿到的仍是它训练时的语言，定位精度不受影响。

字幕的字数区间怎么来的（80~110 个汉字）
---------------------------------------
英文原 prompt 约束是 "not exceed 50 words"。视频时长 ≈ 音频时长 ≈ 讲稿长度，
所以中文必须按**时长**而不是字数对齐：

- 英文版实测：每页约 16.6 s（198.9 s / 12 页）
- 中文 TTS 实测语速：**5.76 汉字/秒**（57 字 → 9.9 s，本机 Qwen3-TTS 实测）
- 反推目标：16.6 s × 5.76 ≈ **96 字**

故取 80~110 字。⚠️ 直接照搬"50 字"会让成片只有应有长度的约六成、语速明显偏快 ——
这是纯配置看不出来、只有对比成片时长才会暴露的坑，验收时必须核对时长。
"""

import os
from os import path

SRC_DIR = path.dirname(path.abspath(__file__))
PKG_ROOT = path.dirname(SRC_DIR)          # Paper2Video-main/


# ──────────────────────────────────────────────────────────────
# 幻灯片语言
# ──────────────────────────────────────────────────────────────
SLIDE_LANGS = {
    "en": {
        "desc": "英文幻灯片",
        "prompt": path.join(SRC_DIR, "prompts", "slide_beamer_prompt.txt"),
        "needs_ctex": False,
    },
    "zh": {
        "desc": "中文幻灯片",
        # 2026-09-18 实现。prompt 里写死了两条**实测得来**的硬约束：
        #   · 导言区必须 \usepackage{ctex} —— tectonic 会自动拉 ctex/xeCJK
        #     与自带的 Fandol 中文字体，不需要装字体、也不需要换引擎；
        #   · **表格必须用 tabularray 的 tblr** —— ctex 与传统 tabular /
        #     booktabs 存在**宏包级冲突**，编译必然报 "Misplaced \noalign"，
        #     且与单元格是否含中文无关（实测纯英文表格同样失败）。
        #     这一条是本次实现的主要障碍，已验证 tblr 在「表格 + 浮动体 +
        #     中文图注 + 图片 + columns」下全部正常。
        "prompt": path.join(SRC_DIR, "prompts", "slide_beamer_prompt_zh.txt"),
        "needs_ctex": True,
    },
}


def _cjk_subtitle_font():
    """中文字幕字体：按平台给默认值，**不要硬编码 Windows 专有字体**。

    为什么必须这样写（2026-09-18 确认迁移需求后加）：
    本项目在使用者本机（Windows）开发，演示通过后**会搬到学校 Linux 服务器**。
    原先这里写死 ``"Microsoft YaHei"``，在 Linux 上字体不存在，libass 会回退到
    ``subtitle_render.DEFAULT_FONT`` —— 而那里的 Linux 分支是 ``DejaVu Sans``，
    **完全不含汉字**，结果就是中文字幕变成方框或直接丢失。

    最阴的一点是：**这个故障在开发机（Windows）上永远不会出现**，
    而且英文讲稿不受影响（DejaVu 覆盖拉丁字母），所以很容易在演示当天才暴露。

    服务器上请确保装了中文字体（Debian/Ubuntu: ``fonts-noto-cjk``），
    或用 ``PAPER2VIDEO_CJK_FONT`` 指定实际可用的字体名。
    """
    if os.name == "nt":
        return "Microsoft YaHei"
    return os.environ.get("PAPER2VIDEO_CJK_FONT", "Noto Sans CJK SC")


# ──────────────────────────────────────────────────────────────
# 讲稿 / TTS / 对齐 / 字幕 语言
# ──────────────────────────────────────────────────────────────
SCRIPT_LANGS = {
    "en": {
        "desc": "英文讲稿",
        "prompt": path.join(SRC_DIR, "prompts", "slide_subtitle_cursor_prompt.txt"),
        "tts_language": "English",
        "align_language": "English",
        "ref_audio": path.join(PKG_ROOT, "assets", "demo", "zeyu.wav"),
        "subtitle_font": None,        # None -> subtitle_render.DEFAULT_FONT（微软雅黑）
        "chars_per_slide": None,      # 由 prompt 里的 "50 words" 约束，代码不重复管
    },
    "zh": {
        "desc": "中文讲稿（配英文幻灯片，即「英文 slides + 中文讲解」）",
        "prompt": path.join(SRC_DIR, "prompts",
                            "slide_subtitle_cursor_prompt_zh.txt"),
        "tts_language": "Chinese",
        "align_language": "Chinese",
        # 中文参考音频是本机用**跨语言零样本克隆**现造的：拿 assets/demo/zeyu.wav
        # （英文）作音色源，让 Qwen3-TTS 用 chinese 合成一段中文，从而中文模式
        # 与英文模式**音色一致**。同名的 .txt 是它的参考文本 ——
        # 有它就不必让 ASR 去转写（ASR 对这段 24k 单声道实测会转错）。
        "ref_audio": path.join(PKG_ROOT, "assets", "demo", "zeyu_zh.wav"),
        "subtitle_font": _cjk_subtitle_font(),   # 见该函数的说明：不能写死 Windows 字体
        "chars_per_slide": (80, 110),   # 见模块 docstring 的推导
    },
}


# ──────────────────────────────────────────────────────────────
# 常用组合预设
# ──────────────────────────────────────────────────────────────
# `--video_lang <name>` 用这里的映射一次性定下两个轴。
VIDEO_PRESETS = {
    "en": {
        "slide_lang": "en",
        "script_lang": "en",
        "desc": "全英文（国际会议 / 投稿）",
    },
    "zh": {
        # ⚠️ 中文预设目前是「英文幻灯片 + 中文讲解」—— 因为中文幻灯片那一路
        # （SLIDE_LANGS['zh']）尚未实现，见其注释里的三项前提。
        # 这也是国内最常用的中文形态，所以先把它做出来。
        "slide_lang": "en",
        "script_lang": "zh",
        "desc": "英文幻灯片 + 中文讲解（国内组会 / 中文答辩）",
    },
}


# ──────────────────────────────────────────────────────────────
# 论文类型（--paper_type）
# ──────────────────────────────────────────────────────────────
# 为什么要有这一层：幻灯片 prompt 里关于「人文社科要不要替换实验章节」的判据，
# 写在模板里只能由**模型自己判断**——那是个软开关。边界论文（例如带实验章节的
# 社会科学论文、或带理论框架图的人文论文）会被判错，而错的方向是「凭空生成实验
# 数据」或「丢掉本该有的实验页」，都很难事后补救。
#
# 所以给出显式档位：`auto` 保持原行为（沿用模板里的软判据），显式指定时
# 由 paper_type_directive() 生成一段**覆盖性指令**追加到 prompt 末尾，
# 用确定性要求压过模型的自由判断。
PAPER_TYPES = {
    "auto": {
        "desc": "由模型自行判断（默认；判据见幻灯片 prompt 模板）",
        "directive": None,
    },
    "research": {
        "desc": "实验型研究：有实验 / 数据集 / 量化结果",
        "directive": {
            "zh": (
                "【本次运行的类型覆盖：以下要求优先于上面关于章节结构的任何描述】\n"
                "已确认源论文属于**实验型研究**（含实验、数据集或量化结果）。"
                "请严格按上面列出的原结构生成，必须保留「方法 / 实验方法 / 实验设置 / "
                "实验结果 / 消融实验」这一类章节。\n"
                "其中所有数据、数据集名、指标名与数值**只能取自源文本**，不得编造；"
                "若源文本确实缺少某一项（例如没做消融实验），就**省略该章节**，"
                "不要用相近内容补造。"),
            "en": (
                "[Paper-type override for this run — this takes precedence over any "
                "chapter-structure description above]\n"
                "The source paper is confirmed to be an EMPIRICAL research paper (it has "
                "experiments, datasets or quantitative results). Follow the original "
                "structure listed above and KEEP the chapters \"Method / Experimental "
                "method / Experimental setting / Experimental results / Ablation "
                "experiment\".\n"
                "All data, dataset names, metric names and numbers must come from the "
                "source text only — invent nothing. If the source genuinely lacks one of "
                "them (e.g. no ablation study was run), OMIT that chapter rather than "
                "filling it with something similar."),
        },
    },
    "humanities": {
        "desc": "人文社科：无实验、无量化数据（如本文艺理论、史学研究）",
        "directive": {
            "zh": (
                "【本次运行的类型覆盖：以下要求优先于上面关于章节结构的任何描述】\n"
                "已确认源论文属于**人文社科类**（无实验、无量化数据）。请只使用这套结构："
                "封面页 / 研究动机 / 相关工作 / 理论框架与概念谱系 / 分析路径与材料 / "
                "案例与文本分析（可分多页）/ 论证的边界与延伸 / 不足之处 / 未来工作 / 结束页。\n"
                "**不要**生成任何「实验方法 / 实验设置 / 实验结果 / 消融实验」章节；"
                "**不要**出现数据集名、指标名或数值、对比表格。"
                "源文本没有的内容一律不写，宁可少一页。"),
            "en": (
                "[Paper-type override for this run — this takes precedence over any "
                "chapter-structure description above]\n"
                "The source paper is confirmed to be a HUMANITIES / social-science paper "
                "(no experiments, no quantitative data). Use ONLY this structure: Title / "
                "Motivation / Related Work / Theoretical framework and conceptual "
                "genealogy / Analytical approach and materials / Case and textual "
                "analysis (may span several pages) / Boundaries and extensions of the "
                "argument / Deficiencies / Future research / End slide.\n"
                "Do NOT generate any \"Experimental method / Experimental setting / "
                "Experimental results / Ablation experiment\" chapter, and do NOT include "
                "dataset names, metric names, numeric values or comparison tables. "
                "Write nothing that is absent from the source; fewer slides is fine."),
        },
    },
}


def paper_type_directive(paper_type, slide_lang="en"):
    """把 ``--paper_type`` 翻成一段**覆盖性指令**（追加到 prompt 末尾）。

    ``auto`` / ``None`` 返回 ``None`` —— 此时完全沿用 prompt 模板里的软判据，
    行为与本层引入之前一致。
    """
    if not paper_type or paper_type == "auto":
        return None
    if paper_type not in PAPER_TYPES:
        raise KeyError("未知的论文类型 {!r}；可选：{}".format(
            paper_type, ", ".join(sorted(PAPER_TYPES))))
    table = PAPER_TYPES[paper_type]["directive"] or {}
    return table.get(slide_lang) or table.get("en")


def resolve(slide_lang=None, script_lang=None, preset=None,
            for_print=True):
    """把语言选择解析成一份完整、可用的配置。

    优先级：显式的 ``slide_lang``/``script_lang`` > ``preset`` > 默认 ``en``。
    返回的 dict 里 ``*_prompt`` / ``ref_audio`` 都已是绝对路径。
    """
    if preset:
        if preset not in VIDEO_PRESETS:
            raise KeyError("未知的语言预设 {!r}；可选：{}".format(
                preset, ", ".join(sorted(VIDEO_PRESETS))))
        combo = VIDEO_PRESETS[preset]
        slide_lang = slide_lang or combo["slide_lang"]
        script_lang = script_lang or combo["script_lang"]
    slide_lang = slide_lang or "en"
    script_lang = script_lang or "en"

    if slide_lang not in SLIDE_LANGS:
        raise KeyError("未知的幻灯片语言 {!r}；可选：{}".format(
            slide_lang, ", ".join(sorted(SLIDE_LANGS))))
    if script_lang not in SCRIPT_LANGS:
        raise KeyError("未知的讲稿语言 {!r}；可选：{}".format(
            script_lang, ", ".join(sorted(SCRIPT_LANGS))))

    slide, script = SLIDE_LANGS[slide_lang], SCRIPT_LANGS[script_lang]

    if slide["prompt"] is None:
        raise NotImplementedError(
            "幻灯片语言 {!r} 尚未实现。要支持它需要：\n"
            "  ① 新建 prompts/slide_beamer_prompt_zh.txt（把原文里的 English "
            "改成中文要求、章节标题中文化）\n"
            "  ② 在该 prompt 里要求 LLM 加 \\usepackage{{ctex}}"
            "（导言区由 LLM 生成，只能靠 prompt 约束）\n"
            "  ③ 实测 VLM 读中文幻灯片图的准确度（唯一未验证环节）\n"
            "目前可用：--slide_lang en（幻灯片英文）+ "
            "--script_lang zh（中文讲解）。".format(slide_lang))

    if not path.isfile(slide["prompt"]):
        raise FileNotFoundError("幻灯片 prompt 不存在：{}".format(slide["prompt"]))
    if not path.isfile(script["prompt"]):
        raise FileNotFoundError("讲稿 prompt 不存在：{}".format(script["prompt"]))
    if not path.isfile(script["ref_audio"]):
        raise FileNotFoundError("参考音频不存在：{}".format(script["ref_audio"]))

    ref_text = _sibling_ref_text(script["ref_audio"])
    if for_print and script_lang == "zh" and slide_lang == "en":
        print("[language] 模式：英文幻灯片 + 中文讲解"
              "（幻灯片保持英文；讲稿/TTS/字幕为中文；光标描述仍为英文，"
              "因为它是喂给 GUI-Owl 的定位指令、不出现在成片里）")

    return {
        "slide_lang": slide_lang,
        "script_lang": script_lang,
        "slide_prompt": slide["prompt"],
        "script_prompt": script["prompt"],
        "tts_language": script["tts_language"],
        "align_language": script["align_language"],
        "ref_audio": script["ref_audio"],
        "ref_text": ref_text,
        "subtitle_font": script["subtitle_font"],
        "chars_per_slide": script["chars_per_slide"],
        "needs_ctex": slide["needs_ctex"],
        "desc": "{}+{}".format(SLIDE_LANGS[slide_lang]["desc"],
                               SCRIPT_LANGS[script_lang]["desc"]),
    }


def _sibling_ref_text(ref_audio):
    """参考音频的同名 .txt 就是它的参考文本（有则用，无则返回 None 交给 ASR）。

    为什么优先用文件而不是让 ASR 转写：实测 Qwen3-ASR 对
    ``assets/demo/zeyu.wav`` 会转出**错的英文**（"To experts discuss about
    sports and politics..." 明显不是原话），而错误的 ref_text 会直接拉低
    零样本克隆的保真度。给了文本就绕开这一步。
    """
    txt = path.splitext(ref_audio)[0] + ".txt"
    if path.isfile(txt):
        with open(txt, encoding="utf-8") as handle:
            text = " ".join(handle.read().split())
        return text or None
    return None


def describe_choices():
    """给 --help 与文档用的一行摘要。"""
    lines = ["语言预设（--video_lang）："]
    for key, val in VIDEO_PRESETS.items():
        lines.append("  {} = {} + {}   {}".format(
            key, val["slide_lang"], val["script_lang"], val["desc"]))
    lines.append("也可用 --slide_lang / --script_lang 单独指定某一轴：")
    lines.append("  幻灯片: " + ", ".join(
        "{}{}".format(k, "" if v["prompt"] else "（未实现）")
        for k, v in SLIDE_LANGS.items()))
    lines.append("  讲稿  : " + ", ".join(SCRIPT_LANGS))
    return "\n".join(lines)
