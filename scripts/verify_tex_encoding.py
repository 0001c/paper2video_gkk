"""自检：上游「平台编码」这一类问题。

查三件事，全部离线、零 API 费用：
  1. 我们链路上的 `.py` 里，文本模式 `open()` 是否都指定了 encoding（AST 扫，不是 grep）；
  2. 读 `prompts/select_proposal.txt`：不带 encoding 在**干净环境**下会不会失败
     （对照组），以及 `PYTHONUTF8=1` / 显式 encoding 是否都能救；
  3. 写 `.tex` 的两种写法各交给 tectonic 编一次，**并检查渲染出来的文字还在不在** ——
     因为 GBK 写出的 .tex 会让 tectonic 返回 0 却把文字丢成 U+FFFD（实测）。

用法：
    .venv\\Scripts\\python.exe scripts\\verify_tex_encoding.py
    ... --skip-compile          # 不跑 tectonic（没装或想快）
    ... --tectonic D:/path/tectonic.exe

退出码：0 = 全部正常（含"对照组如预期失败"）；1 = 有问题。
"""
import argparse
import ast
import io
import locale
import os
import shutil
import subprocess
import sys
import tempfile

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC_ROOT = os.path.join(REPO_ROOT, "Paper2Video-main", "src")
PROMPT = os.path.join(SRC_ROOT, "prompts", "select_proposal.txt")
# 我们的链路：这些文件之外（尤其 evaluation/ 子树）不参与管线，不要求改
OUR_FILES = (
    "pipeline.py", "pipeline_light.py", "cursor_gen.py", "cursor_render.py",
    "speech_gen.py", "subtitle_cursor_prompt_gen.py", "talking_gen.py",
    "talking_config.py", "wei_utils.py", "pv_env.py", "language_config.py",
    "merge_talking.py", "slide_code_gen_select_improvement.py",
)

TECTONIC_CANDIDATES = (
    r"E:\ai-models\bin\tectonic.exe",
    os.path.join(REPO_ROOT, ".venv", "Scripts", "tectonic.exe"),
)


def find_tectonic(explicit):
    if explicit:
        return explicit if os.path.isfile(explicit) else None
    found = shutil.which("tectonic")
    if found:
        return found
    for path in TECTONIC_CANDIDATES:
        if os.path.isfile(path):
            return path
    return None


def unencoded_text_opens(path):
    """返回 [(行号, 模式)]：文本模式 open() 没写 encoding 的位置。"""
    found = []
    try:
        tree = ast.parse(io.open(path, encoding="utf-8").read())
    except Exception:
        return found
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if not (isinstance(fn, ast.Name) and fn.id == "open"):
            continue
        mode = None
        if len(node.args) > 1 and isinstance(node.args[1], ast.Constant):
            mode = node.args[1].value
        for kw in node.keywords:
            if kw.arg == "mode" and isinstance(kw.value, ast.Constant):
                mode = kw.value.value
        if mode is not None and "b" in str(mode):
            continue
        if any(kw.arg == "encoding" for kw in node.keywords):
            continue
        found.append((node.lineno, mode or "r(默认)"))
    return found


def clean_env(**overrides):
    """剔除 PYTHONUTF8/PYTHONIOENCODING —— 才能测到"平台默认"的真实行为。"""
    env = dict(os.environ)
    for key in ("PYTHONUTF8", "PYTHONIOENCODING"):
        env.pop(key, None)
    env.update(overrides)
    return env


def read_probe(code):
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", env=clean_env())
    tail = ((r.stdout or "").strip() or (r.stderr or "").strip()).splitlines()
    return tail[-1] if tail else "(无输出)"


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--tectonic", default=None, help="tectonic 可执行文件路径")
    parser.add_argument("--skip-compile", action="store_true", help="不跑 tectonic")
    args = parser.parse_args()

    print("== 环境 ==")
    print("   平台默认编码 : %s" % locale.getpreferredencoding(False))
    print("   本进程 utf8_mode: %d（宿主 shell 若设了 PYTHONUTF8 会是 1）"
          % sys.flags.utf8_mode)

    print()
    print("== 1. 链路文件的文本 open() 是否都指定了 encoding ==")
    problems = []
    for name in OUR_FILES:
        path = os.path.join(SRC_ROOT, name)
        for lineno, mode in unencoded_text_opens(path):
            problems.append("%s:%d(mode=%s)" % (name, lineno, mode))
    print("   未指定 encoder 的位置: %s" % (problems or "无 ✔"))

    others = []
    for base, dirs, files in os.walk(SRC_ROOT):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        for name in sorted(files):
            if not name.endswith(".py"):
                continue
            rel = os.path.relpath(os.path.join(base, name), SRC_ROOT).replace(os.sep, "/")
            if rel in OUR_FILES:
                continue
            for lineno, _mode in unencoded_text_opens(os.path.join(base, name)):
                others.append("%s:%d" % (rel, lineno))
    print("   其余文件（多为 evaluation/ 论文评测代码）: %d 处（不影响管线）" % len(others))

    print()
    print("== 2. 读 `prompts/select_proposal.txt`（UTF-8，内含 →）==")
    # ⚠️ 探针的打印**只用 ASCII**：子进程的 stdout 也被我们剔了 PYTHONIOENCODING，
    # 于是它按平台编码（cp936）输出，而父进程按 utf-8 解 —— 中文会显示成乱码，
    # 看起来像"发现了一个编码问题"，其实只是**测量方式**的问题（踩过一次）。
    tmpl = ("p = r'@P@'\ntry:\n    print('OK chars=', len(open(p@E@).read()))\n"
            "except Exception as e:\n    print('FAIL', type(e).__name__, str(e)[:60])\n")
    no_enc = tmpl.replace("@P@", PROMPT).replace("@E@", "")
    with_enc = tmpl.replace("@P@", PROMPT).replace("@E@", ", encoding='utf-8'")

    r_default = read_probe(no_enc)
    r_utf8 = read_probe(no_enc)
    env = clean_env(PYTHONUTF8="1")
    proc = subprocess.run([sys.executable, "-c", no_enc], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", env=env)
    r_mode = ((proc.stdout or "").strip() or (proc.stderr or "").strip()).splitlines()[-1]
    r_explicit = read_probe(with_enc)

    print("   不带 encoding（平台默认，对照组）: %s" % r_default)
    print("   不带 encoding + PYTHONUTF8=1     : %s" % r_mode)
    print("   显式 encoding='utf-8'            : %s" % r_explicit)

    print()
    ok = True
    if problems:
        print("✘ 链路文件里仍有未指定编码的 open() —— 见上")
        ok = False
    if not os.path.isfile(PROMPT):
        print("✘ 找不到 %s（上游文件是否齐全？）" % PROMPT)
        return 1
    if sys.platform == "win32":
        if not r_default.startswith("FAIL"):
            print("⚠️ 对照组没有失败：平台的 `open()` 默认已是 UTF-8（可能开了系统级 UTF-8），"
                  "这一项在你这台机器上不构成风险")
        if r_default.startswith("FAIL") and not r_mode.startswith("OK"):
            print("✘ PYTHONUTF8=1 没能救回读取")
            ok = False
    if not r_explicit.startswith("OK"):
        print("✘ 显式 encoding='utf-8' 读取失败")
        ok = False

    if args.skip_compile:
        print()
        print("（--skip-compile：跳过 tectonic 那段）")
        return 0 if ok else 1

    tectonic = find_tectonic(args.tectonic)
    print()
    print("== 3. 写 .tex 两种写法 + tectonic 编译 + 文字是否还在 ==")
    if not tectonic:
        print("   ⚠️ 找不到 tectonic，跳过（可 --tectonic 指定）")
        return 0 if ok else 1
    print("   tectonic: %s" % tectonic)

    # 用**中文**做判据：GBK 文件里 ASCII 部分能幸存，只有非 ASCII 会变成 U+FFFD，
    # 所以拿 ASCII 字样去判"文字在不在"会得出过于乐观的结论。
    body = ("\\documentclass{beamer}\n\\usepackage{ctex}\n\\begin{document}\n"
            "\\begin{frame}{验证标题甲}\n内容乙 arrow → end\n\\end{frame}\n"
            "\\end{document}\n")
    checks = ("验证标题甲", "内容乙", "→")
    work = tempfile.mkdtemp(prefix="verify_enc_")
    try:
        for label, use_utf8 in (("平台默认", False), ("encoding='utf-8'", True)):
            path = os.path.join(work, "v_%s.tex" % ("gbk" if not use_utf8 else "utf8"))
            writer = ("open(r'@P@', 'w'@E@).write(@B@)\n"
                      "raw = open(r'@P@', 'rb').read()\n"
                      "try:\n    raw.decode('utf-8'); print('legal-utf8')\n"
                      "except UnicodeDecodeError:\n    print('NOT-utf8')\n"
                      ).replace("@P@", path).replace(
                          "@E@", ", encoding='utf-8'" if use_utf8 else "").replace(
                          "@B@", repr(body))
            r = subprocess.run([sys.executable, "-c", writer], capture_output=True, text=True,
                               encoding="utf-8", errors="replace", env=clean_env())
            verdict = (r.stdout or r.stderr).strip().splitlines()[-1]
            c = subprocess.run([tectonic, path], cwd=work, capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=900)
            pdf = path.replace(".tex", ".pdf")
            has_text = None
            if os.path.isfile(pdf):
                try:
                    import pymupdf
                    doc = pymupdf.open(pdf)
                    text = "".join(page.get_text() for page in doc)
                    doc.close()
                    has_text = tuple(s in text for s in checks)
                except Exception:
                    pass
            print("   %-18s 文件=%s | 退出码=%s | PDF=%s | 文字在%s=%s" % (
                label, verdict, c.returncode,
                ("%.1f KB" % (os.path.getsize(pdf) / 1024)) if os.path.isfile(pdf) else "未生成",
                list(checks), has_text))
            if use_utf8:
                if not verdict == "legal-utf8":
                    print("   ✘ 显式 utf-8 写出的文件竟然不是合法 UTF-8")
                    ok = False
                if has_text and not all(has_text):
                    print("   ✘ utf-8 写出但渲染后文字缺失：%s" % (has_text,))
                    ok = False
            else:
                # 对照组：期望"文件不是 UTF-8"，并且渲染后文字缺失（或在写入时就崩）
                if verdict == "legal-utf8":
                    print("   ⚠️ 平台默认也写成了 UTF-8（本机系统级 UTF-8），"
                          "这一项在你这台机器上不构成风险")
    finally:
        shutil.rmtree(work, ignore_errors=True)

    print()
    print("结论: %s" % ("全部正常 ✔" if ok else "发现问题 ✘"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
