#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""给 LongCat-Video-Avatar-1.5 打本机（Windows）适配补丁。

## 为什么需要「脚本化」的补丁

两类改动都落在**本仓库的 git 管理之外**：

    代码：E:\\ai-models\\LongCat-Video
    配置：E:\\ai-models\\weights\\LongCat-Video-Avatar-1.5\\base_model_int8\\config.json

手工改一次，目录一旦重建就丢，而且丢失时不会有任何报错 —— 只会在运行时以
「ImportError: No module named triton」这类症状重新出现。故做成可重放的脚本。

用法：

    ./.venv/Scripts/python.exe scripts/patch_longcat.py           # 打补丁
    ./.venv/Scripts/python.exe scripts/patch_longcat.py --check   # 只看状态，不写入

## 补丁 1：把 triton 从顶层依赖改为按需依赖（纯 Python，零编译）

`longcat_video/modules/attention.py:10` 与
`longcat_video/modules/avatar/attention.py:10` 都在**模块顶层**导入
`flash_attn_bsa_3d`，而它的依赖链上还有三处顶层 `import triton`
（`bsa_interface.py` / `common.py` / `flash_attn_bsa_varlen_mask.py`）。

Windows 上没有可用的 triton（`triton-windows` 是社区移植版，与 torch/CPython
版本组合存在风险），于是这几行会让**整个模型 import 不了**。

但配置里 `enable_bsa` 默认是 **false**，BSA 内核永远不会被执行 ——
一个可选功能不该阻断主链路。因此把这两处顶层导入挪到它唯一的使用点
（`if self.enable_bsa and shape[0] > 1:` 分支内部）：
语义完全不变，只是把「加载即要求」改成「用到才要求」。

值得记一笔：整个仓库**没有任何一处用到 xfuser**，`context_parallel` 只依赖
torch + einops。所以 `triton` 是唯一的异类顶层依赖，补丁面就这么大。

## 补丁 2：注意力后端改用 xformers（绕开 flash-attn）

`_process_attn` 是四路分支：

    enable_bsa → enable_flashattn3 → enable_flashattn2 → enable_xformers → else RuntimeError

权重自带的 config 是 `enable_flashattn2=true`，而 `flash_attn==2.7.4.post1`
在 Windows 上没有官方 wheel、需要现场编译 —— **这正是本项目从 InfiniteTalk
换型过来的直接原因**（InfiniteTalk 的 fp8 加载要求现场编译 quanto 的 CUDA 扩展，
而本机 nvcc 12.6 没有 compute_120 目标）。

xformers 有官方 win_amd64 wheel（0.0.35），且上游代码本来就支持这条分支，
故改用它。上游 README 也明确写了可以换：「you can also change the model config
to use FlashAttention-3 or xformers」。
"""

import argparse
import ast
import json
import os
import sys

DEFAULT_ROOT = os.environ.get("LONGCAT_ROOT", r"E:\ai-models\LongCat-Video")
DEFAULT_WEIGHTS = os.environ.get(
    "LONGCAT_AVATAR_WEIGHTS",
    r"E:\ai-models\weights\LongCat-Video-Avatar-1.5")

# bsa 分支的锚点行（两个文件里完全一致，且各自只出现一次相关的调用）
BSA_BRANCH = ("        if self.enable_bsa and shape[0] > 1: "
              "# bsa will not be used in image training / sampling\n")

TOP_IMPORT_NOTE = (
    "# 上游这行是**模块顶层**导入：\n"
    "#   from {dots}block_sparse_attention.bsa_interface import flash_attn_bsa_3d\n"
    "# 而该模块链上有三处顶层 `import triton`，Windows 上没有可用的 triton，\n"
    "# 于是整个模型 import 不了 —— 但 enable_bsa 默认 false，BSA 内核永不执行。\n"
    "# 故改为在下方 bsa 分支内惰性导入（语义不变，只是延后到真正用到时）。\n"
    "# 由 scripts/patch_longcat.py 自动生成，勿手改。\n"
)

LAZY_IMPORT_NOTE = "  # noqa: E402 惰性导入：仅 enable_bsa 时才会执行"


def build_text_patches():
    """返回 [(相对路径, 原文, 新文, 说明)]，顺序即应用顺序。"""
    patches = []
    for rel, dots in (
        (os.path.join("longcat_video", "modules", "attention.py"), ".."),
        (os.path.join("longcat_video", "modules", "avatar", "attention.py"), "..."),
    ):
        imp = "from {0}block_sparse_attention.bsa_interface import flash_attn_bsa_3d".format(dots)
        # 先删顶层导入，再在分支内补上（顺序敏感）
        patches.append((rel, imp + "\n", TOP_IMPORT_NOTE.format(dots=dots),
                        "移除顶层 bsa 导入"))
        patches.append((rel, BSA_BRANCH,
                        BSA_BRANCH + "            " + imp + LAZY_IMPORT_NOTE + "\n",
                        "在 bsa 分支内补惰性导入"))
    return patches


# ── xformers -> SDPA 兜底补丁 ──────────────────────────────────────
# 两个 attention.py 的 `elif self.enable_xformers:` 分支完全一致，都在末尾
# 调用 `xformers.ops.memory_efficient_attention(...)`。问题：xformers 0.0.35
# 的 CUDA kernel 只编到 sm_90，在 Blackwell(sm_120) 上 dispatch 会报
# `NotImplementedError: No operator found ... capability (12, 0) (too new)`。
# 而 torch 自带的 `F.scaled_dot_product_attention` 原生支持 bf16 + sm_120，
# 且这正是 InfiniteTalk 当初缺 flash-attn 时的退化路径。故在 xformers 调用
# 外包一层 try/except，失败时降级到 SDPA（语义等价，只是少了 memory-efficient）。
_XFORMERS_CALL_OLD = (
    "            x = xformers.ops.memory_efficient_attention(q, k, v, attn_bias=None, op=None,)\n"
)
_XFORMERS_CALL_NEW = (
    "            try:\n"
    "                x = xformers.ops.memory_efficient_attention(q, k, v, attn_bias=None, op=None,)\n"
    "            except (NotImplementedError, RuntimeError):\n"
    "                # Blackwell(sm_120) 上 xformers 0.0.35 的 kernel 未编入（仅到 sm_90），\n"
    "                # 降级到 torch 原生 SDPA。注意 SDPA 要 [B, H, S, D]，而此处 q/k/v 已被\n"
    "                # rearrange 成 xformers 的 [B, S, H, D]，故先 transpose 回 B H S D；\n"
    "                # SDPA 支持 q 与 kv 序列长度不同的 cross-attention（q_noise 那路）。\n"
    "                x = torch.nn.functional.scaled_dot_product_attention(\n"
    "                    q.transpose(1, 2).contiguous(),\n"
    "                    k.transpose(1, 2).contiguous(),\n"
    "                    v.transpose(1, 2).contiguous(),\n"
    "                ).transpose(1, 2)\n"
)


def build_sdpa_patches():
    """两个 attention.py 各自加一条 xformers -> SDPA 的兜底。"""
    return [
        (os.path.join("longcat_video", "modules", "attention.py"),
         _XFORMERS_CALL_OLD, _XFORMERS_CALL_NEW, "xformers 失败时降级 SDPA"),
        (os.path.join("longcat_video", "modules", "avatar", "attention.py"),
         _XFORMERS_CALL_OLD, _XFORMERS_CALL_NEW, "xformers 失败时降级 SDPA"),
    ]


def apply_text_patch(path, old, new, dry_run):
    if not os.path.isfile(path):
        return "文件不存在"
    text = open(path, encoding="utf-8").read()
    # 先判「新」再判「旧」：new 里含 old 时（注释版）判定才不会失效
    if new in text:
        return "已打过"
    if old not in text:
        return "未找到预期文本（上游可能已改版）"
    if dry_run:
        return "需要改写"
    open(path, "w", encoding="utf-8", newline="").write(text.replace(old, new))
    return "已改写"


def check_syntax(path):
    try:
        ast.parse(open(path, encoding="utf-8").read())
        return None
    except SyntaxError as exc:
        return "{}:{}".format(exc.lineno, exc.msg)


def patch_attention_config(config_path, dry_run):
    """把注意力后端从 flashattn2 切到 xformers。

    刻意**不做 JSON 反序列化再序列化**：原文件是 4 空格缩进，若用
    ``json.dump(indent=2)`` 重排会把 853 字节变成 893 字节，而
    ``download_models.py --verify`` 是按**字节大小**比对的，会因此误报「有问题」。

    改用等长文本替换：``true`` -> ``false`` 加 1 字节、``false`` -> ``true``
    减 1 字节，净变化恰好为 0，文件大小不变，校验照样通过。
    """
    if not os.path.isfile(config_path):
        return "配置不存在: {}".format(config_path)
    text = open(config_path, encoding="utf-8").read()

    fa_on, fa_off = '"enable_flashattn2": true', '"enable_flashattn2": false'
    xf_off, xf_on = '"enable_xformers": false', '"enable_xformers": true'

    if fa_off in text and xf_on in text:
        return "已是 xformers（flashattn2=false / xformers=true）"
    if fa_on not in text or xf_off not in text:
        return "未找到预期字段（上游可能已改版）"
    if dry_run:
        return "需要改写（当前 flashattn2=true / xformers=false）"

    new_text = text.replace(fa_on, fa_off).replace(xf_off, xf_on)
    if len(new_text) != len(text):
        return "等长替换失败：{} -> {} 字节".format(len(text), len(new_text))
    open(config_path, "w", encoding="utf-8", newline="").write(new_text)

    # 复核：既要能被解析，也确实是目标取值
    cfg = json.loads(new_text)
    if cfg.get("enable_flashattn2") is not False or cfg.get("enable_xformers") is not True:
        return "改写后复核不通过"
    return "已切到 xformers（等长替换，{} 字节不变）".format(len(new_text))


def main():
    parser = argparse.ArgumentParser(description="给 LongCat-Video-Avatar-1.5 打本机适配补丁")
    parser.add_argument("--root", default=DEFAULT_ROOT, help="LongCat-Video 代码仓库根目录")
    parser.add_argument("--weights", default=DEFAULT_WEIGHTS,
                        help="LongCat-Video-Avatar-1.5 权重目录")
    parser.add_argument("--check", action="store_true", help="只检查，不写入")
    args = parser.parse_args()

    root, weights, dry = args.root, args.weights, args.check
    print("代码仓库：{}".format(root))
    print("权重目录：{}".format(weights))
    if not os.path.isdir(root):
        print("代码仓库不存在。请先克隆 meituan-longcat/LongCat-Video。")
        return 2

    ok = True
    print()
    print("[1/3] 把 triton 依赖改为按需（2 个文件，共 4 处）")
    touched = set()
    for rel, old, new, desc in build_text_patches():
        path = os.path.join(root, rel)
        state = apply_text_patch(path, old, new, dry)
        print("      {:<44} {:<12} {}".format(
            "{} [{}]".format(rel, desc[:6]), state, desc))
        if state not in ("已打过", "已改写", "需要改写"):
            ok = False
        touched.add(path)

    print()
    print("[2/4] 注意力后端：flashattn2 -> xformers（配置）")
    config_path = os.path.join(weights, "base_model_int8", "config.json")
    state = patch_attention_config(config_path, dry)
    print("      {}".format(state))
    if "不存在" in state:
        ok = False

    print()
    print("[3/4] xformers 失败时降级 SDPA（Blackwell 上 xformers kernel 未编入）")
    for rel, old, new, desc in build_sdpa_patches():
        path = os.path.join(root, rel)
        state = apply_text_patch(path, old, new, dry)
        print("      {:<44} {:<12} {}".format(rel, state, desc))
        if state not in ("已打过", "已改写", "需要改写"):
            ok = False
        touched.add(path)

    # [4/4] 部署单卡精简推理入口 avatar_infer.py（替代有 Linux/分布式硬伤的 demo）
    infer_src = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "longcat_patches", "avatar_infer.py")
    infer_dst = os.path.join(root, "avatar_infer.py")
    print()
    print("[4/4] 部署精简推理入口 avatar_infer.py")
    if not os.path.isfile(infer_src):
        print("      源缺失：{}".format(infer_src))
        ok = False
    elif dry:
        print("      {} -> {}（--check 模式，未写入）".format(
            os.path.basename(infer_src), os.path.basename(infer_dst)))
    else:
        import shutil as _shutil
        _shutil.copyfile(infer_src, infer_dst)
        print("      已部署 {} -> {}".format(infer_src, infer_dst))

    # 语法自检：文本替换很容易把缩进改坏，必须当场拦住
    print()
    print("[自检] 被改动文件的语法")
    for path in sorted(touched):
        err = check_syntax(path)
        rel = os.path.relpath(path, root)
        if err:
            print("      [语法错误] {} -> {}".format(rel, err))
            ok = False
        else:
            print("      [OK] {}".format(rel))

    print()
    if dry:
        print("（--check 模式：未写入任何文件）")
    else:
        print("补丁完成。" if ok else "补丁部分失败，请查看上面的提示。")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
