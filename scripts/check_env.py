"""环境自检：一次性列清「还缺什么」。

Phase 0 的验收工具。检查项分五类：
1. Python 与关键 Python 包
2. GPU / CUDA / 计算能力（sm_120 是 Blackwell 的硬门槛）
3. 外部可执行文件（ffmpeg / LaTeX / poppler）
4. 模型权重是否就位
5. .env 配置是否填了真实 Key

用法：
    ./.venv/Scripts/python.exe scripts/check_env.py
    ./.venv/Scripts/python.exe scripts/check_env.py --json     # 机器可读
"""

import os
import sys
import json
import shutil
import argparse
import subprocess

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(PROJECT_ROOT, "Paper2Video-main", "src"))

# (显示名, import 名, 是否必需, 用途)
PY_PACKAGES = [
    ("torch", "torch", True, "所有本地模型"),
    ("transformers", "transformers", True, "GUI-Owl / Qwen3-TTS 加载"),
    ("qwen-tts", "qwen_tts", True, "语音合成"),
    ("qwen-asr", "qwen_asr", True, "转写 + 强制对齐"),
    ("modelscope", "modelscope", True, "CAM++ 声纹模型下载"),
    ("huggingface_hub", "huggingface_hub", True, "权重下载"),
    ("soundfile", "soundfile", True, "音频读写"),
    ("opencv-python", "cv2", True, "幻灯片图像处理"),
    # PyMuPDF 替代 pdf2image + poppler：纯 pip 安装，无需外部二进制
    ("PyMuPDF", "fitz", True, "PDF -> PNG（原代码 pdf2img 也用它）"),
    ("pdf2image", "pdf2image", False, "仅作 PyMuPDF 缺失时的回退（需 poppler）"),
    ("camel-ai", "camel", True, "原仓库的 LLM 调用框架"),
    ("openai", "openai", True, "DeepSeek（OpenAI 兼容）调用"),
    ("python-dotenv", "dotenv", False, "可选的 .env 加载（本仓库已内置零依赖解析）"),
    ("websockets", "websockets", False, "数字人阶段可能用到"),
]

# (可执行文件, 是否必需, 用途, 缺失后果)
EXECUTABLES = [
    ("ffmpeg", True, "合片、字幕烧录、光标叠加", "Stage 3 合片全部无法执行"),
    ("ffprobe", True, "读取音视频时长", "合片时无法确定每页停留时长"),
    ("pdflatex", True, "把 slides.tex 编译成 PDF", "Stage 1 幻灯片生成无法完成"),
    ("pdftoppm", True, "PDF -> PNG（pdf2image 依赖 poppler）", "幻灯片无法转成图片"),
]

# 模型权重：(repo, 分组, 约 GB)
MODEL_WEIGHTS = [
    ("Qwen/Qwen3-TTS-12Hz-1.7B-Base", "phase1", 4.5),
    ("Qwen/Qwen3-ASR-0.6B", "phase1", 1.9),
    ("Qwen/Qwen3-ForcedAligner-0.6B", "phase1", 1.8),
    ("mPLUG/GUI-Owl-1.5-4B-Instruct", "phase1", 8.9),
    ("iic/speech_campplus_sv_zh-cn_16k-common", "eval", 0.03),
    ("Wan-AI/Wan2.1-I2V-14B-480P", "phase3", 33.0),
    ("MeiGen-AI/InfiniteTalk", "phase3", 12.0),
    ("TencentGameMate/chinese-wav2vec2-base", "phase3", 0.4),
]

OK, WARN, BAD = "[OK]", "[!!]", "[XX]"


def check_python_packages(results):
    print("\n── Python 包 ──")
    for display, module, required, purpose in PY_PACKAGES:
        try:
            mod = __import__(module)
            version = getattr(mod, "__version__", "?")
            print("  {} {:<20} {:<12} {}".format(OK, display, str(version)[:12], purpose))
            results["packages"][display] = {"ok": True, "version": str(version)}
        except Exception as exc:
            tag = BAD if required else WARN
            print("  {} {:<20} {:<12} {}".format(tag, display, "缺失", purpose))
            results["packages"][display] = {"ok": False, "required": required,
                                            "error": type(exc).__name__}


def check_torch_cuda(results):
    print("\n── GPU / CUDA ──")
    try:
        import torch
    except ImportError:
        print("  {} torch 未安装，跳过 CUDA 检查".format(BAD))
        results["cuda"] = {"ok": False, "reason": "torch missing"}
        return

    print("  {} torch {}".format(OK, torch.__version__))
    cuda_version = torch.version.cuda
    if not torch.cuda.is_available():
        print("  {} CUDA 不可用（torch 编译版本: {}）".format(BAD, cuda_version))
        results["cuda"] = {"ok": False, "reason": "cuda unavailable"}
        return

    capability = torch.cuda.get_device_capability()
    name = torch.cuda.get_device_name(0)
    sm = "{}{}".format(*capability)
    print("  {} CUDA {}".format(OK, cuda_version))
    print("  {} 设备 {}  compute capability sm_{}".format(OK, name, sm))

    # sm_120 是本机硬门槛：cu118 的旧 wheel 不含该编译目标
    if capability[0] >= 12:
        if "cu12" in str(cuda_version):
            print("  {} sm_{} 需要 cu128 及以上，当前 {} 满足".format(OK, sm, cuda_version))
        else:
            print("  {} sm_{} 需要 cu128 及以上，当前 {} **可能不含该目标**".format(BAD, sm, cuda_version))
    results["cuda"] = {"ok": True, "cuda": cuda_version, "name": name, "capability": list(capability)}

    # 显存
    try:
        free, total = torch.cuda.mem_get_info()
        used = (total - free) / 1e9
        print("  {} 显存 {:.1f} GB 可用 / {:.1f} GB 总计（已占用 {:.1f} GB）".format(
            OK, free / 1e9, total / 1e9, used))
        if free / 1e9 < 10:
            print("  {} 可用显存不足 10 GB —— 跑大模型前建议先关掉占显存的程序".format(WARN))
        results["cuda"]["vram_free_gb"] = round(free / 1e9, 2)
        results["cuda"]["vram_total_gb"] = round(total / 1e9, 2)
    except Exception as exc:
        print("  {} 显存查询失败: {}".format(WARN, exc))


def check_executables(results):
    print("\n── 外部工具 ──")
    for name, required, purpose, consequence in EXECUTABLES:
        found = shutil.which(name)
        if found:
            print("  {} {:<12} {}".format(OK, name, found))
            results["executables"][name] = {"ok": True, "path": found}
        else:
            tag = BAD if required else WARN
            print("  {} {:<12} 未找到 —— {}：{}".format(tag, name, purpose, consequence))
            results["executables"][name] = {"ok": False, "required": required,
                                            "consequence": consequence}


def check_weights(results):
    import pv_env
    print("\n── 模型权重 ──")
    print("  权重根目录: {}".format(pv_env.WEIGHTS_DIR))
    if not os.path.isdir(pv_env.WEIGHTS_DIR):
        print("  {} 目录不存在，请运行 scripts/download_models.py".format(BAD))

    for repo, group, approx in MODEL_WEIGHTS:
        local = os.path.join(pv_env.WEIGHTS_DIR, repo.replace("/", "__"))
        if os.path.isdir(local):
            size = 0
            for root, _dirs, files in os.walk(local):
                for f in files:
                    try:
                        size += os.path.getsize(os.path.join(root, f))
                    except OSError:
                        pass
            ratio = size / (approx * 1e9) if approx else 0
            tag = OK if ratio > 0.9 else WARN
            print("  {} [{:<6}] {:<44} {:>6.2f} GB".format(tag, group, repo, size / 1e9))
            results["weights"][repo] = {"ok": ratio > 0.9, "size_gb": round(size / 1e9, 2)}
        else:
            tag = OK if group == "phase3" else WARN
            note = "（Phase 3，暂不需要）" if group == "phase3" else "（需下载）"
            print("  {} [{:<6}] {:<44} 缺失 {}".format(tag, group, repo, note))
            results["weights"][repo] = {"ok": False, "group": group}


def check_env_config(results):
    import pv_env
    print("\n── API 配置 ──")
    print("  .env 位置: {}".format(pv_env.REPO_ROOT / ".env"))
    for key, required in (("OPENAI_API_KEY", True), ("OPENAI_API_BASE", True),
                          ("DASHSCOPE_API_KEY", False)):
        value = os.environ.get(key, "")
        masked = (value[:6] + "..." + value[-4:]) if len(value) > 12 else "(空)"
        if value:
            print("  {} {:<20} {}".format(OK, key, masked))
            results["env"][key] = {"ok": True}
        else:
            tag = BAD if required else WARN
            hint = "DeepSeek 必需" if key.startswith("OPENAI") else "仅视频理解环节需要"
            print("  {} {:<20} 未配置（{}）".format(tag, key, hint))
            results["env"][key] = {"ok": False, "required": required}


def check_disk(results):
    print("\n── 磁盘 ──")
    for drive in ("C:/", "E:/", "F:/"):
        try:
            total, _used, free = shutil.disk_usage(drive)
            tag = WARN if free < 30e9 and drive.startswith("C") else OK
            print("  {} {} 剩余 {:.0f} GB".format(tag, drive, free / 1e9))
            results["disk"][drive] = round(free / 1e9, 1)
        except Exception:
            pass
    print("  提示：权重放 E 盘（SSD，随机读快）；F 盘（HDD）只建议放数据集归档")


def main():
    parser = argparse.ArgumentParser(description="Paper2Video 环境自检")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    args = parser.parse_args()

    results = {"packages": {}, "executables": {}, "weights": {}, "env": {}, "disk": {}}

    if not args.json:
        print("=" * 78)
        print("Paper2Video 环境自检")
        print("=" * 78)
        print("Python: {} ({})".format(sys.version.split()[0], sys.executable))

    check_python_packages(results)
    check_torch_cuda(results)
    check_executables(results)
    check_weights(results)
    check_env_config(results)
    check_disk(results)

    # ── 汇总 ──
    blockers = []
    for name, info in results["packages"].items():
        if not info.get("ok") and info.get("required"):
            blockers.append("Python 包缺失: {}".format(name))
    for name, info in results["executables"].items():
        if not info.get("ok") and info.get("required"):
            blockers.append("外部工具缺失: {}".format(name))
    if not results.get("cuda", {}).get("ok"):
        blockers.append("CUDA 不可用（sm_120 需要 torch cu128+）")
    for key, info in results["env"].items():
        if not info.get("ok") and info.get("required"):
            blockers.append("API 配置缺失: {}".format(key))

    if not args.json:
        print("\n" + "=" * 78)
        if blockers:
            print("阻塞项 {} 个：".format(len(blockers)))
            for item in blockers:
                print("  - {}".format(item))
        else:
            print("Phase 0 环境就绪，无阻塞项。")
        print("=" * 78)

    if args.json:
        results["blockers"] = blockers
        print(json.dumps(results, ensure_ascii=False, indent=2))

    return 1 if blockers else 0


if __name__ == "__main__":
    sys.exit(main())
