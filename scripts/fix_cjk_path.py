"""把「因中文路径而无法加载」的 Python 包镜像到纯英文目录。

问题
----
项目位于 ``E:\\Development\\26年\\paper2video``，路径含中文（``26年``）。

Python 自身的文件操作走 Windows 宽字符 API，含中文没问题；但**编译型扩展
在 C/C++ 层用窄字符路径打开文件时会失败**。实测：

    import nagisa
    RuntimeError: Could not read model from
        E:\\Development\\26年\\paper2video\\.venv\\Lib\\site-packages\\nagisa\\data\\nagisa_v001.model

nagisa 的模型加载由 DyNet（C++）实现，它读不了这个路径。把同一份包复制到
``E:\\ai-models\\vendor`` 后导入与分词均正常 —— 根因确认是路径而非包本身。

为什么必须修
------------
``qwen_asr.inference.qwen3_forced_aligner`` 在**模块顶层** ``import nagisa``，
而这个依赖只在 ``language == "japanese"`` 时才会被调用。也就是说，
一个可选语言的支持把整个 ``qwen_asr`` 包拖成了不可导入 —— 不做处理的话
Qwen3-ASR 与 Qwen3-ForcedAligner 都跑不起来。

做法
----
1. 把受影响的包从 site-packages 复制到 ``E:\\ai-models\\vendor``（纯英文）；
2. 由 ``pv_env`` 在导入期把该目录前置到 ``sys.path``，使 ``import nagisa``
   命中英文副本（见 pv_env._prepend_vendor_path）。

这样既是完全可用的真包（不是 stub，日文对齐照常可用），又不依赖任何
site-packages 内的 hack，重建 venv 后重跑本脚本即可恢复。

用法
----
    ./.venv/Scripts/python.exe scripts/fix_cjk_path.py            # 复制并自检
    ./.venv/Scripts/python.exe scripts/fix_cjk_path.py --check    # 只检查现状
"""

import argparse
import os
import shutil
import subprocess
import sys

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VENV_SITE = os.path.join(PROJECT_ROOT, ".venv", "Lib", "site-packages")
VENDOR_DIR = os.environ.get("PAPER2VIDEO_VENDOR", r"E:\ai-models\vendor")

# 需要英文镜像的模块。每条：(包目录名, 附带的顶层模块 glob)
NEEDS_ASCII = [
    # nagisa 的数据文件由 DyNet(C++) 按窄字符路径打开 -> 含中文路径必然失败
    ("nagisa", ["nagisa_utils.*.pyd", "nagisa_utils.py"]),
]


def _is_ascii(path):
    try:
        str(path).encode("ascii")
        return True
    except UnicodeEncodeError:
        return False


def _copytree(src, dst):
    if os.path.isdir(dst):
        shutil.rmtree(dst, ignore_errors=True)
    shutil.copytree(src, dst)


def _glob_top(site, pattern):
    """在 site-packages 顶层按通配符找文件（不递归）。"""
    import fnmatch
    hits = []
    for name in os.listdir(site):
        if fnmatch.fnmatch(name, pattern):
            hits.append(name)
    return hits


def mirror_one(pkg, extras, check_only=False):
    src = os.path.join(VENV_SITE, pkg)
    if not os.path.isdir(src):
        print("  [跳过] {}：site-packages 里没有这个包".format(pkg))
        return None

    dst = os.path.join(VENDOR_DIR, pkg)
    # 已经镜像过且内容一致就跳过（用源目录内最大文件的 mtime 做粗略指纹）
    if os.path.isdir(dst):
        print("  [已存在] {}".format(dst))
        if check_only:
            return dst

    if check_only:
        print("  [待镜像] {} -> {}".format(pkg, dst))
        return None

    print("  [镜像] {} -> {}".format(pkg, dst))
    _copytree(src, dst)
    for pattern in extras:
        for name in _glob_top(VENV_SITE, pattern):
            shutil.copy(os.path.join(VENV_SITE, name), VENDOR_DIR)
            print("         + {}".format(name))
    return dst


def verify():
    """在子进程里前置 vendor 目录后导入，确认修复生效。"""
    if not os.path.isdir(VENDOR_DIR):
        print("  vendor 目录不存在，无法验证")
        return False

    code = (
        "import sys\n"
        "sys.path.insert(0, r'{vendor}')\n"
        "import nagisa\n"
        "print('NAGISA_OK', getattr(nagisa, '__version__', '?'), "
        "nagisa.tagging('これはテストです').words)\n"
    ).format(vendor=VENDOR_DIR)

    proc = subprocess.run([sys.executable, "-X", "utf8", "-c", code],
                          capture_output=True, text=True, timeout=180)
    out = (proc.stdout or "") + (proc.stderr or "")
    if "NAGISA_OK" in out:
        for line in out.splitlines():
            if "NAGISA_OK" in line:
                print("  [OK] {}".format(line.strip()))
        return True
    print("  [FAIL] 前置 {} 后仍导入失败：".format(VENDOR_DIR))
    print("        " + out.strip().replace("\n", "\n        ")[:900])
    return False


def main():
    # global 声明必须出现在函数体内对该名字的任何引用之前，否则 SyntaxError。
    # （add_argument 的 default=VENDOR_DIR 就已经算一次引用了。）
    global VENDOR_DIR

    parser = argparse.ArgumentParser(description="把受中文路径影响的包镜像到英文目录")
    parser.add_argument("--check", action="store_true", help="只报告，不复制")
    parser.add_argument("--vendor", default=VENDOR_DIR, help="镜像目标目录")
    args = parser.parse_args()

    VENDOR_DIR = args.vendor

    print("项目路径 : {}".format(PROJECT_ROOT))
    print("venv     : {}".format(VENV_SITE))
    print("镜像目录 : {}".format(VENDOR_DIR))
    print("项目路径含非 ASCII: {}".format(not _is_ascii(PROJECT_ROOT)))
    print()

    if not os.path.isdir(VENV_SITE):
        print("找不到 site-packages：{}（请先创建 .venv）".format(VENV_SITE))
        return 1

    if not args.check:
        os.makedirs(VENDOR_DIR, exist_ok=True)

    print("[镜像]")
    made = []
    for pkg, extras in NEEDS_ASCII:
        if mirror_one(pkg, extras, args.check):
            made.append(pkg)

    if args.check:
        return 0

    print("\n[验证] 前置镜像目录后导入")
    ok = verify()

    print()
    if ok:
        print("完成。pv_env 会在导入期自动把 {} 前置到 sys.path。".format(VENDOR_DIR))
        return 0
    print("验证未通过。可先手动确认问题包能否从英文路径导入：")
    print("  {}\\Scripts\\python.exe -c \"import sys;sys.path.insert(0,r'{}');import nagisa;print(nagisa.__version__)\"".format(
        os.path.join(PROJECT_ROOT, ".venv"), VENDOR_DIR))
    return 1


if __name__ == "__main__":
    sys.exit(main())
