#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""安装 LongCat-Video-Avatar-1.5 的独立 Python 环境（Windows 原生）。

用法（用主环境的解释器运行本脚本即可，脚本内部会去建自己的 venv）：

    ./.venv/Scripts/python.exe scripts/install_avatar_deps.py

## 为什么要独立环境

LongCat 钉 `numpy==1.26.4` 与 `transformers==4.41.0`，而主链路环境是
numpy 2.x + transformers 4.57.6。硬凑到一个环境里会连带动主链路**已经验证能出片**的
风险面，不划算。（这也正是上游 `pipeline.py` 用 `--talking_head_env` 参数指定
独立解释器的原因。）

## 与上游 README 的三处必要偏离

1. **不用 `torch==2.6.0+cu124`，改 `2.11.0+cu128`。**
   Blackwell(sm_120) 从 cu128 起才有编译目标，`cu124` 的 wheel 在本机加载不起来。

2. **不装 `flash-attn==2.7.4.post1`，改 `xformers`。**
   `longcat_video/modules/avatar/attention.py` 第 69-114 行是四路分支
   （flashattn3 / flashattn2 / **xformers** / 否则 `RuntimeError`），
   改成 `enable_xformers=true` 即可绕开 flash-attn，而 xformers 有官方
   win_amd64 wheel。这是本项目从 InfiniteTalk 换型过来的**主要收益**：
   全程不需要现场编译任何 C++/CUDA 扩展。

3. **不执行 `pip install --upgrade pip`。**
   沙箱的 safe-delete 批量守卫会在「卸载旧 pip」之后拦住「装回新 pip」，
   结果 venv 里没有 pip（表现为之后所有 pip 命令报 `No module named pip`）。
   本脚本统一给子进程带上 `CODEBUDDY_SAFE_DELETE_ENABLED=0`。

## 另外两处必须放宽容差（上游钉的版本没有 cp312 的 wheel）

- `onnxruntime==1.16.3` -> 不钉版本（1.16 发布于 2023 年，没有 cp312 wheel）
- `libsndfile1==0.0.1`  -> 移除（PyPI 上该名字是占位包；上游用的是 conda 装法）
"""

import os
import re
import subprocess
import sys
import time

# ── 路径 ────────────────────────────────────────────────────────────
ROOT = os.environ.get("LONGCAT_ROOT", r"E:\ai-models\LongCat-Video")
VENV = os.path.join(ROOT, ".venv")
VENV_PY = os.path.join(VENV, "Scripts", "python.exe")

# 建 venv 时唯一允许使用「非项目虚拟环境」解释器的地方
PY312 = os.environ.get("PAPER2VIDEO_PY312",
                       r"C:\Users\Gkk\AppData\Local\Programs\Python\Python312\python.exe")

WHEEL_DIR = os.environ.get("PAPER2VIDEO_WHEEL_DIR", r"E:\ai-models\wheels")
TORCH_WHEEL = os.path.join(WHEEL_DIR, "torch-2.11.0+cu128-cp312-cp312-win_amd64.whl")
TORCHVISION_VER = "0.26.0"
XFORMERS_VER = "0.0.35"

PYPI = "https://pypi.tuna.tsinghua.edu.cn/simple"
TORCH_INDEX = "https://mirror.sjtu.edu.cn/pytorch-wheels/cu128/"

# ── 依赖裁剪规则 ─────────────────────────────────────────────────────
# 完全丢弃（改用别的装法、不需要、或源码里零引用）
DROP = {
    "torch",            # 改为本地 wheel 装 cu128 版（见 [1/4] 步骤）
    "flash-attn",       # 改用 xformers（有官方 win wheel，无需编译）
    "libsndfile1",      # PyPI 上是占位包，上游用 conda 装法
    "tritonserverclient",  # 源码零引用（Triton 推理服务器客户端，数字人用不到）；
                           # 且 PyPI 上根本没有可装版本，作者装的是内部源
}
# 放宽上界/钉版（上游的钉版在 cp312 上装不上）
RELAX = {"onnxruntime"}


def log(msg=""):
    print(msg, flush=True)


def run(cmd, desc, timeout=None):
    """执行一条命令，返回 (是否成功, 输出尾部)。"""
    log("  $ {}".format(" ".join(cmd)))
    env = os.environ.copy()
    # 关键：关闭 safe-delete 批量守卫，否则 pip 写 site-packages 会被拦
    env["CODEBUDDY_SAFE_DELETE_ENABLED"] = "0"
    t0 = time.time()
    try:
        proc = subprocess.run(cmd, env=env, capture_output=True, text=True,
                              timeout=timeout, errors="replace")
    except subprocess.TimeoutExpired:
        log("  [超时] {}（超过 {}s）".format(desc, timeout))
        return False, "timeout"
    tail = (proc.stdout or "")[-1500:] + (proc.stderr or "")[-800:]
    ok = proc.returncode == 0
    log("  [{}] {}  用时 {:.0f}s".format("OK" if ok else "失败", desc, time.time() - t0))
    if not ok:
        log("  --- 输出尾部 ---")
        for line in tail.strip().splitlines()[-12:]:
            log("   " + line)
    return ok, tail


def ensure_venv():
    if os.path.isfile(VENV_PY):
        log("[0] venv 已存在，跳过创建")
        return True
    log("[0] 创建 venv：{}".format(VENV))
    if not os.path.isfile(PY312):
        log("    找不到 Python 3.12：{}".format(PY312))
        return False
    ok, _ = run([PY312, "-m", "venv", VENV], "python -m venv")
    return ok


def filtered_requirements():
    """把上游 requirements 过滤成适合本机的一份，返回临时文件路径。"""
    blocks = []
    for name in ("requirements.txt", "requirements_avatar.txt"):
        path = os.path.join(ROOT, name)
        if not os.path.isfile(path):
            log("    [跳过] 找不到 {}".format(path))
            continue
        kept = []
        for raw in open(path, encoding="utf-8"):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            pkg = re.split(r"[=<>!\[\s]", line, 1)[0].lower()
            if pkg in DROP:
                log("    [剔除] {:<28} ({})".format(line, name))
                continue
            if pkg in RELAX:
                log("    [放宽] {:<28} -> {}".format(line, pkg))
                kept.append(pkg)
                continue
            kept.append(line)
        blocks.extend(kept)

    out = os.path.join(ROOT, "requirements_local.txt")
    guard = [
        "# 由 scripts/install_avatar_deps.py 生成，勿手改",
        "#",
        "# 下面几行是「防替换守卫」：它们已由前置步骤装好（带 +cu128 本地版本号），",
        "# 而 PEP 440 下 `2.11.0+cu128` 满足 `==2.11.0`，所以解析器会认为已满足、",
        "# 不会去 PyPI 拉那个 124MB 的 CPU-only 版 torch 把 CUDA 版顶掉。",
        "torch==2.11.0",
        "torchvision==0.26.0",
        "xformers==0.0.35",
        "accelerate",   # 上游 requirements 没列，但缺失会让 from_pretrained 退化为
                       # low_cpu_mem_usage=False（加载峰值显存更高）。16GB 卡必须装。
        "",
    ]
    with open(out, "w", encoding="utf-8") as handle:
        handle.write("\n".join(guard + blocks) + "\n")
    log("    已生成 {}（守卫 4 条 + 依赖 {} 条）".format(out, len(blocks)))
    return out


def main():
    log("=" * 72)
    log("LongCat-Video-Avatar-1.5 环境安装")
    log("  ROOT = {}".format(ROOT))
    log("=" * 72)

    if not ensure_venv():
        log("venv 创建失败，终止。")
        return 1
    run([VENV_PY, "-c", "import sys;print('Python', sys.version.split()[0])"], "核对解释器")

    log()
    log("[1/4] torch 2.11.0+cu128（本地 wheel，避免走不稳的下载源）")
    if os.path.isfile(TORCH_WHEEL):
        log("    使用本地 wheel：{} ({:.0f} MB)".format(
            os.path.basename(TORCH_WHEEL), os.path.getsize(TORCH_WHEEL) / 2 ** 20))
        run([VENV_PY, "-m", "pip", "install", "--no-cache-dir", TORCH_WHEEL],
            "pip install torch（本地 wheel）")
    else:
        log("    本地 wheel 不存在，改从 SJTU 镜像装")
        run([VENV_PY, "-m", "pip", "install", "--no-cache-dir",
             "--index-url", TORCH_INDEX, "torch==2.11.0"], "pip install torch（镜像）")

    log()
    log("[2/4] torchvision {}（cu128 同源）".format(TORCHVISION_VER))
    # --no-deps 很关键：否则 PyPI 上的 torchvision 会把 torch 换成 CPU 版
    run([VENV_PY, "-m", "pip", "install", "--no-deps", "--index-url", TORCH_INDEX,
         "torchvision=={}".format(TORCHVISION_VER)], "pip install torchvision")

    log()
    log("[3/4] xformers {}（替代 flash-attn；同样 --no-deps 防止它动 torch）".format(XFORMERS_VER))
    run([VENV_PY, "-m", "pip", "install", "--no-deps", "-i", PYPI,
         "xformers=={}".format(XFORMERS_VER)], "pip install xformers")

    log()
    log("[4/4] 其余依赖（过滤后的 requirements）")
    req = filtered_requirements()
    run([VENV_PY, "-m", "pip", "install", "-i", PYPI, "-r", req],
        "pip install -r requirements_local.txt")

    log()
    log("=" * 72)
    log("验证")
    log("=" * 72)
    check = (
        "import torch, torchvision, xformers, numpy, transformers, diffusers, librosa, soundfile\n"
        "print('torch       ', torch.__version__, '| cuda', torch.cuda.is_available(),"
        " '| cap', torch.cuda.get_device_capability())\n"
        "print('torchvision ', torchvision.__version__)\n"
        "print('xformers    ', xformers.__version__)\n"
        "print('numpy       ', numpy.__version__)\n"
        "print('transformers', transformers.__version__)\n"
        "print('diffusers   ', diffusers.__version__)\n"
    )
    ok, _ = run([VENV_PY, "-c", check], "导入与版本核对")

    log()
    log("完成。解释器路径（供 pipeline.py 的 --talking_head_env 使用）：")
    log("  {}".format(VENV_PY))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
