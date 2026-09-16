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

# ── 关于 transformers 的版本死结（实测确认，勿改）──
#
#   qwen-tts 全系 6 个版本 (0.0.2~0.1.1) 都钉 transformers==4.57.3
#   qwen-asr 全系 6 个版本 (0.0.1~0.0.6) 都钉 transformers==4.57.6
#
# 两者都是**精确相等**约束，pip 无解（ResolutionImpossible）：
# 同一解释器里不可能同时满足。没有"换个版本就对齐"的组合。
#
# 取舍依据 —— 装 4.57.6 这一侧，原因有两条：
#   1. 4.57.6 更新。qwen-asr 发布时(1/30)特意钉 4.57.6，说明它**需要**
#      该版本带来的修复；而 qwen-tts(2/6 发布，晚了 7 天)仍钉 4.57.3，
#      更像是模板复制留下的陈旧 pin，而非真实依赖。
#   2. 只在 patch 段内跨越 (.3 -> .6)。语义化版本下 patch 仅含 bugfix，
#      不引入破坏性 API 变更。
#
# 因此：transformers 钉 4.57.6，qwen-tts 用 --no-deps 装（见 install_qwen_pkgs），
# 再靠 verify() 里的**真实推理自检**兜底 —— 版本推断可能有偏差，
# 跑一次 generate_voice_clone 才是决定性证据。
TRANSFORMERS_PIN = "transformers==4.57.6"

# 主依赖组。qwen-tts / qwen-asr 不在这里，单独装（见下）。
OTHER_PACKAGES = [
    TRANSFORMERS_PIN,
    "tokenizers",
    "accelerate==1.12.0",
    "safetensors",
    # camel-ai 声明 numpy<=2.2，且 numpy 2.3+ 移除了部分旧 API。
    # 注意：torch 2.11 的 wheel 会顺手把 numpy 拉到 2.5.x，必须在 torch 之后压回来。
    "numpy<2.3",
    # ── 音频 ──
    "soundfile",
    "librosa",
    "torchaudio",
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

# qwen-tts 用 --no-deps 安装，因此它自己的运行期依赖必须在这里显式补齐，
# 否则会因为缺包而在推理时炸掉。逐条来自其 METADATA 的 requires_dist。
QWEN_TTS_OWN_DEPS = ["librosa", "soundfile", "torchaudio", "accelerate==1.12.0"]


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


def install_qwen_pkgs(use_mirror=True):
    """装 qwen-asr（正常）与 qwen-tts（--no-deps，绕开 transformers 死结）。

    qwen-asr 要的 transformers==4.57.6 与已装版本一致，可正常解析。
    qwen-tts 要的是 4.57.3，直接装会触发 ResolutionImpossible，
    故剥掉它的依赖声明，靠主依赖组与 QWEN_TTS_OWN_DEPS 兜住运行期依赖。
    """
    mirror = ["-i", PYPI_MIRROR] if use_mirror else []

    ok = _pip(["install", "--progress-bar", "off"] + mirror + ["qwen-asr"],
              "qwen-asr（Qwen3-ASR + Qwen3-ForcedAligner）")

    ok = _pip(["install", "--progress-bar", "off"] + mirror
              + ["--no-deps", "qwen-tts"],
              "qwen-tts（--no-deps 绕开 transformers 精确 pin 冲突）") and ok

    ok = _pip(["install", "--progress-bar", "off"] + mirror + QWEN_TTS_OWN_DEPS,
              "qwen-tts 自身依赖（因 --no-deps 需手动补齐）") and ok
    return ok


def verify(run_inference=False):
    """装完立刻验证最关键的几件事。

    run_inference=True 时额外跑一次真实 TTS 前向 —— 这是判断
    「transformers 4.57.6 能否带动 qwen-tts」的决定性证据：
    版本号推断只到概率层面，跑通才算数。
    """
    print("\n" + "=" * 70)
    print("[验证] 依赖导入")
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
        # numpy 上限是 camel-ai 的硬约束，torch 2.11 的 wheel 会把它顶到 2.5.x
        "try:\n"
        "    import numpy\n"
        "    print('numpy', numpy.__version__)\n"
        "    assert numpy.__version__ < '2.3', 'numpy 过高，camel-ai 会报错'\n"
        "except Exception as e:\n"
        "    print('FAIL numpy:', type(e).__name__, e)\n"
        "for mod in ['qwen_tts','qwen_asr','cv2','fitz','soundfile','librosa','camel','openai','modelscope']:\n"
        "    try:\n"
        "        __import__(mod); print('OK  ', mod)\n"
        "    except Exception as e:\n"
        "        print('MISS', mod, type(e).__name__)\n"
    )
    subprocess.run([VENV_PYTHON, "-c", code])

    print("\n[依赖一致性] pip check")
    subprocess.run([VENV_PYTHON, "-m", "pip", "check"])

    if run_inference:
        run_tts_smoke_test()


def run_tts_smoke_test():
    """真实跑一次 Qwen3-TTS 音色克隆，验证 transformers 4.57.6 的兼容性。

    这是 Phase 1 里最不确定的一环（两包依赖声明互斥、只能强行跨越），
    必须用真实前向判定 —— import 成功说明不了任何事。
    """
    print("\n" + "=" * 70)
    print("[验证] Qwen3-TTS 真实推理自检")
    print("=" * 70)
    code = (
        "import os, sys\n"
        "import numpy as np, soundfile as sf\n"
        "sys.path.insert(0, os.path.join(os.getcwd(), 'Paper2Video-main', 'src'))\n"
        "import pv_env\n"
        "from speech_gen import TTS_REPO, get_tts_model, get_clone_prompt\n"
        "name = pv_env.model_path(TTS_REPO)\n"
        "print('模型目录:', name)\n"
        "assert os.path.isdir(name), '权重目录不存在，请先跑 scripts/download_models.py'\n"
        "# 造 3 秒参考音频（衰减包络的正弦），只为验证前向链路\n"
        "sr0 = 24000\n"
        "t = np.linspace(0, 3.0, sr0 * 3, endpoint=False)\n"
        "env = np.minimum(1.0, np.minimum(t * 5, (3.0 - t) * 5))\n"
        "sf.write('_smoke_ref.wav', (0.2 * np.sin(2 * np.pi * 220 * t) * env).astype(np.float32), sr0)\n"
        "model = get_tts_model()\n"
        "print('OK   模型加载')\n"
        "prompt = get_clone_prompt('_smoke_ref.wav', 'Hello world.')\n"
        "print('OK   音色 prompt 构建')\n"
        "wavs, sr = model.generate_voice_clone(text='This is a smoke test.',\n"
        "                                      language='English', voice_clone_prompt=prompt)\n"
        "print('OK   前向成功: %d 段波形, 采样率 %s, 首段 %.2f 秒' % (len(wavs), sr, len(wavs[0]) / sr))\n"
        "sf.write('_smoke_out.wav', wavs[0], sr)\n"
        "print('RESULT: Qwen3-TTS 在 transformers 4.57.6 下可用')\n"
    )
    result = subprocess.run([VENV_PYTHON, "-c", code])
    if result.returncode == 0:
        print("[i] 自检产物 _smoke_ref.wav / _smoke_out.wav 在项目根目录，可试听后删除")
    else:
        print("[!] 推理自检未通过 —— 需评估是否把 TTS 拆到独立虚拟环境")


def main():
    parser = argparse.ArgumentParser(description="安装 Phase 1 依赖")
    parser.add_argument("--skip-torch", action="store_true", help="跳过 torch（已装好时）")
    parser.add_argument("--only-torch", action="store_true", help="只装 torch")
    parser.add_argument("--skip-qwen", action="store_true", help="跳过 qwen-tts / qwen-asr")
    parser.add_argument("--no-mirror", action="store_true", help="不走国内 PyPI 镜像")
    parser.add_argument("--smoke", action="store_true",
                        help="额外跑一次真实 TTS 推理（需先下载权重）")
    args = parser.parse_args()

    _ensure_venv()

    ok = True
    if not args.skip_torch:
        ok = install_torch() and ok
    if not args.only_torch:
        ok = install_others(use_mirror=not args.no_mirror) and ok
        if not args.skip_qwen:
            ok = install_qwen_pkgs(use_mirror=not args.no_mirror) and ok

    verify(run_inference=args.smoke)
    print("\n完成。" if ok else "\n存在失败项，请查看上面的输出。")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
