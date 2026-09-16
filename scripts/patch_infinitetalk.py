"""给 InfiniteTalk 打本机适配补丁（可重复执行，幂等）。

为什么需要「脚本化补丁」而不是手工改文件
----------------------------------------
InfiniteTalk 的代码在 `E:\\ai-models\\InfiniteTalk`，**不在本项目的 git 仓库里**，
手工改动不受版本管理、重建环境就丢。把补丁固化成脚本后：

- 重装/换机时一条命令复原；
- 改了什么、为什么改，全部有据可查；
- 幂等，重复执行不会重复插入。

补丁内容
--------
1. 复制 `wan/xfuser_compat.py`（源文件在本仓库
   `scripts/infinitetalk_patches/xfuser_compat.py`）。
2. 把两个**模块顶层**导入 xfuser 的文件改为导入兼容层：

   - `wan/modules/attention.py`
   - `wan/utils/multitalk_utils.py`

   原因：`import xfuser` 会连带 `import triton`，而 **triton 没有 Windows wheel**，
   一个只为可选稀疏注意力服务的依赖会把整个模型拖成不可导入。

不改 `wan/distributed/xdit_context_parallel.py`：它只在多卡分支里被导入
（4 处 `if use_usp:` 内），单卡根本加载不到；而且它还依赖真实的
`xFUserLongContextAttention`，本来就只能在多卡环境用。

用法
----
    python scripts/patch_infinitetalk.py            # 打补丁
    python scripts/patch_infinitetalk.py --check    # 只检查是否已打
"""

import argparse
import os
import re
import shutil
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PATCH_SRC = os.path.join(PROJECT_ROOT, "scripts", "infinitetalk_patches",
                         "xfuser_compat.py")

DEFAULT_ROOT = r"E:\ai-models\InfiniteTalk"

# 需要改导入的两个文件（相对 InfiniteTalk 根目录）
TARGET_FILES = [
    os.path.join("wan", "modules", "attention.py"),
    os.path.join("wan", "utils", "multitalk_utils.py"),
]

# 纯文本替换型补丁：(相对路径, 原文, 新文, 说明)
TEXT_PATCHES = [
    (
        os.path.join("wan", "multitalk.py"),
        "from inspect import ArgSpec\n",
        "# 上游这行 `from inspect import ArgSpec` 是**未被使用的死导入**\n"
        "# （全仓库仅此一处出现），而 inspect.ArgSpec 在 Python 3.11 已被移除，\n"
        "# 于是它把 wan 包整体变成不可导入 —— 上游文档推荐 Python 3.10 正是此缘故。\n"
        "# from inspect import ArgSpec\n",
        "注释掉 Python 3.11 已移除的 ArgSpec 死导入",
    ),
]

# 上游的原始导入块（顶层、无缩进）
ORIGINAL_IMPORT = re.compile(
    r"from xfuser\.core\.distributed import \(\s*"
    r"get_sequence_parallel_rank,\s*"
    r"get_sequence_parallel_world_size,\s*"
    r"get_sp_group,\s*\)",
    re.S,
)

PATCHED_IMPORT = (
    "from ..xfuser_compat import (\n"
    "    get_sequence_parallel_rank,\n"
    "    get_sequence_parallel_world_size,\n"
    "    get_sp_group,\n"
    ")"
)


def patch_module(dest, dry_run=False):
    """复制兼容层模块。"""
    if not os.path.isfile(PATCH_SRC):
        raise FileNotFoundError("找不到补丁源文件：{}".format(PATCH_SRC))
    src_text = open(PATCH_SRC, encoding="utf-8").read()
    if os.path.isfile(dest) and open(dest, encoding="utf-8").read() == src_text:
        return "已是最新"
    if dry_run:
        return "需要写入"
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    shutil.copyfile(PATCH_SRC, dest)
    return "已写入"


def patch_imports(target, dry_run=False):
    """重写目标文件的 xfuser 导入。"""
    if not os.path.isfile(target):
        return "文件不存在，跳过"
    text = open(target, encoding="utf-8").read()

    if "from ..xfuser_compat import" in text:
        return "已打过补丁"
    if not ORIGINAL_IMPORT.search(text):
        return "未找到预期的导入块（上游可能已改版，请人工确认）"
    if dry_run:
        return "需要改写"

    patched = ORIGINAL_IMPORT.sub(PATCHED_IMPORT, text, count=1)
    with open(target, "w", encoding="utf-8") as handle:
        handle.write(patched)
    return "已改写"


def apply_text_patch(target, old, new, dry_run=False):
    """做一次精确文本替换。"""
    if not os.path.isfile(target):
        return "文件不存在，跳过"
    text = open(target, encoding="utf-8").read()
    if old not in text:
        # 已经打过（新文在，原文不在）还是上游改版了？
        return "已打过补丁" if new in text else "未找到预期文本（上游可能已改版）"
    if dry_run:
        return "需要改写"
    with open(target, "w", encoding="utf-8") as handle:
        handle.write(text.replace(old, new, 1))
    return "已改写"


def main():
    parser = argparse.ArgumentParser(description="给 InfiniteTalk 打本机适配补丁")
    parser.add_argument("--root", default=os.environ.get("PAPER2VIDEO_INFINITETALK_ROOT")
                        or DEFAULT_ROOT, help="InfiniteTalk 仓库根目录")
    parser.add_argument("--check", action="store_true", help="只检查，不写入")
    args = parser.parse_args()

    root = args.root
    print("InfiniteTalk 根目录：{}".format(root))
    if not os.path.isdir(root):
        print("目录不存在。请先克隆 InfiniteTalk，或用 --root 指定。")
        return 2

    dry = args.check
    print()
    print("[1/3] 兼容层模块 wan/xfuser_compat.py")
    print("      -> {}".format(
        patch_module(os.path.join(root, "wan", "xfuser_compat.py"), dry)))

    print()
    print("[2/3] 改写顶层 xfuser 导入")
    ok = True
    for rel in TARGET_FILES:
        state = patch_imports(os.path.join(root, rel), dry)
        print("      {:<34} {}".format(rel, state))
        if "未找到" in state or "不存在" in state:
            ok = False

    print()
    print("[3/3] Python 3.11+ 兼容性补丁")
    for rel, old, new, desc in TEXT_PATCHES:
        state = apply_text_patch(os.path.join(root, rel), old, new, dry)
        print("      {:<34} {}（{}）".format(rel, state, desc))
        if "未找到" in state or "不存在" in state:
            ok = False

    print()
    if dry:
        print("（--check 模式：未写入任何文件）")
    else:
        print("补丁完成。" if ok else "补丁部分失败，请查看上面的提示。")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
