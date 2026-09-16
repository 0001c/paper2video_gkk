"""一键安装 Phase 1（主链路）依赖。

为什么要单独写脚本而不是一份 requirements.txt
------------------------------------------------
PyTorch 的 cu128 版本必须从 ``download.pytorch.org`` 的专用索引安装
（本机 RTX 5070 Ti 是 sm_120，cu118 的 wheel 不含该编译目标 —— 这也是
原仓库锁死的 Hallo2 环境在本机跑不起来的根因）。而其余包应走 PyPI 镜像。
一个 requirements.txt 里混两个索引容易造成依赖劫持，所以拆成两步显式安装。

用法：
    ./.venv/Scripts/python.exe scripts/install_deps.py
    ./.venv/Scripts/python.exe scripts/install_deps.py --skip-torch   # torch 已装好时
    ./.venv/Scripts/python.exe scripts/install_deps.py --no-mirror   # 不走国内镜像
"""

import os
import sys
import argparse
import subprocess

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VENV_PYTHON = os.path.join(PROJECT_ROOT, ".venv", "Scripts", "python.exe")

TORCH_INDEX = "https://download.pytorch.org/whl/cu128"
PYPI_MIRROR = "https://pypi.tuna.tsinghua.edu.cn/simple"

TORCH_PACKAGES = ["torch", "torchvision", "torchaudio"]

# 其余依赖。分组注释说明每个包为什么必须存在。
#
# ⚠️ transformers 必须钉死版本，不能写 >=：
#   qwen-asr 0.0.6 要求 transformers==4.57.6
#   qwen-tts 0.1.1 要求 transformers==4.57.3
#   两个包都用了精确相等约束，而 PyPI 上最新是 5.x —— 若放任 pip 解析，
#   会装上 5.x，qwen-tts / qwen-asr 双双失效。这里钉在 4.57.6，
#   实测 4.57.x 段内 API 兼容，两个包可共存（差异仅 patch 号）。
#   camel-ai 要求 transformers<5，也在这个区间内，无冲突。
OTHER_PACKAGES = [
    # ── 模型框架（版本敏感，见上）──
    "transformers==4.57.6",
    "tokenizers",
    "accelerate==1.12.0",
    "safetensors",
    # camel-ai 声明 numpy<=2.2，且 numpy 2.3+ 移除了部分旧 API
    "numpy<2.3",
    # ── 本地模型 ──
    "qwen-tts",                  # Qwen3-TTS-12Hz 推理
    "qwen-asr",                  # Qwen3-ASR + Qwen3-ForcedAligner 推理
    # ── 音频 ──
    "soundfile",
    "librosa",
    # ── 图像 / PDF ──
    "opencv-python",
    "PyMuPDF",                   # 替代 pdf2image + poppler
    "Pillow",
    # ── 权重下载 ──
    "huggingface_hub",
    "modelscope",                # CAM++ 声纹模型走 ModelScope
    # ── LLM 调用（DeepSeek 走 OpenAI 兼容协议）──
    "camel-ai>=0.2.0",
    "openai>=1.86.0",
    # ── 基础 ──
    "tqdm",
]

SKIP_IF_INSTALLED = {"numpy", "Pillow"}


def _ensure_venv():
    """确保用项目 venv 的 python 执行，而不是全局解释器。"""
    if not os.path.isfile(VENV_PYTHON):
        print("[!] 未找到虚拟环境 {}，请先创建：".format(VENV_PYTHON))
        print("    python -m venv .venv")
        sys.exit(1)
    if os.path.normcase(os.path.abspath(sys.executable)) != os.path.normcase(os.path.abspath(VENV_PYTHON)):
        print("[i] 切换到项目虚拟环境: {}".format(VENV_PYTHON))
        os.execv(VENV_PYTHON, [VENV_PYTHON, os.path.abspath(__file__)] + sys.argv[1:])


def _pip(args, label):
    print("\n" + "=" * 70)
    print("[安装] {}".format(label))
    print("=" * 70)
    result = subprocess.run([VENV_PYTHON, "-m", "pip"] + args)
    if result.returncode != 0:
        print("[!] 失败: {}".format(label))
        return False
    return True


def install_torch():
    return _pip(
        ["install", "--progress-bar", "off",
         "--index-url", TORCH_INDEX] + TORCH_PACKAGES,
        "PyTorch cu128（sm_120 必需）",
    )


def install_others(use_mirror=True):
    args = ["install", "--progress-bar", "off"]
    if use_mirror:
        args += ["-i", PYPI_MIRROR]
    args += OTHER_PACKAGES
    return _pip(args, "主链路其余依赖")


def verify():
    """装完立刻验证最关键的几件事。"""
    print("\n" + "=" * 70)
    print("[验证]")
    print("=" * 70)
    code = (
        "import sys\n"
        "print('python', sys.version.split()[0])\n"
        "try:\n"
        "    import torch\n"
        "    print('torch', torch.__version__, 'cuda', torch.version.cuda)\n"
        "    if torch.cuda.is_available():\n"
        "        cap = torch.cuda.get_device_capability()\n"
        "        print('device', torch.cuda.get_device_name(0), 'sm_%d%d' % cap)\n"
        "        assert cap[0] >= 12, 'compute capability 低于 12，cu128 未生效'\n"
        "        print('OK   sm_120 可用')\n"
        "    else:\n"
        "        print('WARN CUDA 不可用')\n"
        "except Exception as e:\n"
        "    print('FAIL torch:', type(e).__name__, e)\n"
        # transformers 版本是这套依赖里最脆弱的一环：qwen-tts 与 qwen-asr 各钉一个 patch 号
        "try:\n"
        "    import transformers\n"
        "    print('transformers', transformers.__version__)\n"
        "except Exception as e:\n"
        "    print('FAIL transformers:', type(e).__name__, e)\n"
        # GUI-Owl 依赖 Qwen3-VL 的模型类，这是 transformers 4.57+ 才引入的
        "try:\n"
        "    from transformers import Qwen3VLForConditionalGeneration\n"
        "    print('OK   Qwen3VLForConditionalGeneration 可用（GUI-Owl 依赖）')\n"
        "except Exception as e:\n"
        "    print('FAIL Qwen3VL:', type(e).__name__, e)\n"
        "for mod in ['qwen_tts','qwen_asr','cv2','fitz','soundfile','camel','openai','modelscope']:\n"
        "    try:\n"
        "        __import__(mod); print('OK  ', mod)\n"
        "    except Exception as e:\n"
        "        print('MISS', mod, type(e).__name__)\n"
    )
    subprocess.run([VENV_PYTHON, "-c", code])

    print("\n[依赖一致性] pip check")
    subprocess.run([VENV_PYTHON, "-m", "pip", "check"])


def main():
    parser = argparse.ArgumentParser(description="安装 Phase 1 依赖")
    parser.add_argument("--skip-torch", action="store_true", help="跳过 torch（已装好时）")
    parser.add_argument("--only-torch", action="store_true", help="只装 torch")
    parser.add_argument("--no-mirror", action="store_true", help="不走国内 PyPI 镜像")
    args = parser.parse_args()

    _ensure_venv()

    ok = True
    if not args.skip_torch:
        ok = install_torch() and ok
    if not args.only_torch:
        ok = install_others(use_mirror=not args.no_mirror) and ok

    verify()
    print("\n完成。" if ok else "\n存在失败项，请查看上面的输出。")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
