"""统一下载 Paper2Video 所需的全部模型权重。

设计要点
--------
1. **一律下载到纯英文路径**（``E:\\ai-models\\weights``）。项目本身位于
   ``E:\\Development\\26年\\...``，路径含中文，而 Wan / InfiniteTalk 系的加载器
   遇到非 ASCII 路径会直接抛错，所以权重必须与代码目录解耦。
2. **优先 ModelScope，回退 HuggingFace 镜像**。国内 ModelScope 带宽通常显著
   优于 hf-mirror；GUI-Owl 目前只有 HF 有，脚本会自动回退。
3. 支持 ``--group`` 分组下载，先拿主链路（phase1，约 17 GB），
   数字人链路（phase3，80 GB+）等前面跑通再下。

用法
----
    ./.venv/Scripts/python.exe scripts/download_models.py --list
    ./.venv/Scripts/python.exe scripts/download_models.py --group phase1
    ./.venv/Scripts/python.exe scripts/download_models.py --model Qwen/Qwen3-ASR-0.6B
    ./.venv/Scripts/python.exe scripts/download_models.py --group phase3 --source modelscope
"""

import os
import sys
import argparse

# 控制台按 UTF-8 输出，避免 Windows GBK 下中文/特殊符号报错
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEIGHTS_DIR = os.environ.get("PAPER2VIDEO_WEIGHTS", r"E:\ai-models\weights")

# 每个条目：repo_id -> (分组, 说明, 约体积GB, 首选源)
# 说明里的体积来自 HF API 的 siblings 实测，仅含该仓库自身文件（不含底模）。
CATALOG = {
    # ── Phase 1：主链路（无数字人）──
    "Qwen/Qwen3-TTS-12Hz-1.7B-Base": (
        "phase1", "语音合成：零样本音色克隆（替换 F5-TTS）", 4.5, "modelscope"),
    "Qwen/Qwen3-ASR-0.6B": (
        "phase1", "语音转写：52 语言/方言（替换 WhisperX 转写部分）", 1.9, "modelscope"),
    "Qwen/Qwen3-ForcedAligner-0.6B": (
        "phase1", "强制对齐：词级时间戳，误差 27.8ms（替换 WhisperX 对齐部分）", 1.8, "modelscope"),
    "mPLUG/GUI-Owl-1.5-4B-Instruct": (
        "phase1", "光标空间定位：输出 0-1000 归一化坐标（替换 UI-TARS-1.5-7B）", 8.9, "huggingface"),

    # ── 评测链路 ──
    "iic/speech_campplus_sv_zh-cn_16k-common": (
        "eval", "音色相似度：CAM++ 说话人验证（替换 SpeechBrain ECAPA）", 0.03, "modelscope"),

    # ── Phase 3：数字人（体量大，前面跑通再下）──
    "Wan-AI/Wan2.1-I2V-14B-480P": (
        "phase3", "InfiniteTalk 底模：Wan2.1 图生视频 14B", 33.0, "modelscope"),
    "MeiGen-AI/InfiniteTalk": (
        "phase3", "音频驱动数字人 adapter（只取 single/ 与必要文件，仓库全量 168GB）", 12.0, "modelscope"),
    "TencentGameMate/chinese-wav2vec2-base": (
        "phase3", "InfiniteTalk 音频编码器", 0.4, "modelscope"),
}

# InfiniteTalk 仓库里只需要这几个前缀，其余（comfyui/、quant_models/ 的 int8/fp8
# 变体）加起来 150 GB，全是可选的旁支，默认不拉。
INFINITETALK_ALLOW = ["single/", "README.md", ".gitattributes"]


def _human(num_bytes):
    for unit in ("B", "KB", "MB", "GB"):
        if num_bytes < 1024:
            return "{:.1f} {}".format(num_bytes, unit)
        num_bytes /= 1024
    return "{:.1f} TB".format(num_bytes)


def _dir_size(path):
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass
    return total


def download_modelscope(repo, local_dir):
    """走 ModelScope。返回 True 表示成功。"""
    try:
        from modelscope import snapshot_download
    except ImportError:
        print("    [跳过] 未安装 modelscope（pip install modelscope）")
        return False

    kwargs = {"model_id": repo, "local_dir": local_dir}
    if repo == "MeiGen-AI/InfiniteTalk":
        kwargs["allow_file_pattern"] = INFINITETALK_ALLOW
    snapshot_download(**kwargs)
    return True


def download_huggingface(repo, local_dir):
    """走 huggingface_hub（HF_ENDPOINT 指向 hf-mirror）。返回 True 表示成功。"""
    try:
        from huggingface_hub import snapshot_download
    except ImportError:
        print("    [跳过] 未安装 huggingface_hub")
        return False

    snapshot_download(
        repo_id=repo,
        local_dir=local_dir,
        resume_download=True,
        max_workers=4,
    )
    return True


def fetch_one(repo, source="auto"):
    group, desc, approx_gb, preferred = CATALOG[repo]
    local_dir = os.path.join(WEIGHTS_DIR, repo.replace("/", "__"))

    print("\n" + "=" * 78)
    print("[{}] {}".format(group, repo))
    print("    用途：{}".format(desc))
    print("    预计体积：约 {:.1f} GB".format(approx_gb))
    print("    目标目录：{}".format(local_dir))

    if os.path.isdir(local_dir) and _dir_size(local_dir) > approx_gb * 1e9 * 0.9:
        print("    状态：已存在且体积达标，跳过")
        return True

    order = []
    if source == "auto":
        order = [preferred, "huggingface" if preferred == "modelscope" else "modelscope"]
    else:
        order = [source]

    for src in order:
        print("    尝试源：{}".format(src))
        try:
            ok = (download_modelscope if src == "modelscope" else download_huggingface)(repo, local_dir)
        except Exception as exc:
            print("    失败：{}: {}".format(type(exc).__name__, str(exc)[:200]))
            continue
        if ok:
            print("    完成，实际体积 {:.2f} GB".format(_dir_size(local_dir) / 1e9))
            return True

    print("    ✗ 全部源均失败")
    return False


def main():
    parser = argparse.ArgumentParser(description="下载 Paper2Video 模型权重")
    parser.add_argument("--group", choices=["phase1", "eval", "phase3", "all"],
                        help="按分组下载")
    parser.add_argument("--model", action="append", help="指定 repo_id，可重复")
    parser.add_argument("--source", choices=["auto", "modelscope", "huggingface"], default="auto")
    parser.add_argument("--list", action="store_true", help="只列出清单")
    parser.add_argument("--weights-dir", default=WEIGHTS_DIR)
    args = parser.parse_args()

    global WEIGHTS_DIR
    WEIGHTS_DIR = args.weights_dir

    if args.list or (not args.group and not args.model):
        print("权重根目录：{}\n".format(WEIGHTS_DIR))
        print("{:<44} {:<8} {:>8}  {}".format("repo", "group", "size", "说明"))
        print("-" * 130)
        for repo, (group, desc, gb, _src) in CATALOG.items():
            print("{:<44} {:<8} {:>6.1f}G  {}".format(repo, group, gb, desc))
        total = sum(v[2] for v in CATALOG.values())
        print("-" * 130)
        print("{:<44} {:<8} {:>6.1f}G  （phase1 小计 {:.1f}G）".format(
            "合计", "", total, sum(v[2] for k, v in CATALOG.items() if v[0] == "phase1")))
        return

    targets = []
    if args.model:
        targets.extend(args.model)
    elif args.group == "all":
        targets.extend(CATALOG.keys())
    else:
        targets.extend([k for k, v in CATALOG.items() if v[0] == args.group])

    unknown = [t for t in targets if t not in CATALOG]
    if unknown:
        print("未知 repo（请先用 --list 查看）：{}".format(unknown))
        sys.exit(2)

    print("共 {} 个仓库，目标目录 {}".format(len(targets), WEIGHTS_DIR))
    os.makedirs(WEIGHTS_DIR, exist_ok=True)

    results = {}
    for repo in targets:
        results[repo] = fetch_one(repo, args.source)

    print("\n" + "=" * 78)
    ok = [r for r, v in results.items() if v]
    bad = [r for r, v in results.items() if not v]
    print("成功 {} 个，失败 {} 个".format(len(ok), len(bad)))
    for repo in bad:
        print("  ✗ {}".format(repo))
    sys.exit(0 if not bad else 1)


if __name__ == "__main__":
    main()
