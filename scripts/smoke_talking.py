"""数字人（InfiniteTalk）阶段冒烟测试 —— 只跑单页，用于快速验证环境是否可用。

为什么单独一个脚本
------------------
主链路的 `pipeline.py --stage '["3"]'` 会把 12 页音频依次跑一遍；而数字人是整个
流水线里最重、最容易在环境层面失败的一环（显存、量化权重、CUDA 目标、专用 venv）。
先用单页把「环境是否可用」和「单页要多久」这两个问题问清楚，再去跑整片，
可以避免把时间浪费在「跑到第 5 页才发现第 1 页就是错的」上。

用法
----
    # 本机默认档位，用第 0 页音频
    python scripts/smoke_talking.py

    # 指定页与档位
    python scripts/smoke_talking.py --idx 2 --profile local-16g

    # 查看可用档位
    python scripts/smoke_talking.py --list-profiles

注意：本脚本必须用**主环境**的解释器运行（它负责调度），它内部会去调
InfiniteTalk 自己的专用 venv（numpy<2 决定了那边必须是 Python 3.12）。
"""

import argparse
import os
import subprocess
import sys
import time

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC_DIR = os.path.join(PROJECT_ROOT, "Paper2Video-main", "src")
sys.path.insert(0, SRC_DIR)
os.chdir(SRC_DIR)          # 与 pipeline.py 的运行目录保持一致

import pv_env  # noqa: E402

from talking_config import describe_profile, list_profiles, resolve_profile  # noqa: E402
from talking_gen import resolve_paths, run_infinitetalk_inference  # noqa: E402

DEFAULT_REF_IMG = os.path.join("..", "assets", "demo", "zeyu.png")
DEFAULT_RESULT = os.path.join("..", "..", "result", "zeyu")


def check_prerequisites(paths):
    """先做静态检查，把「环境没装好」和「模型跑挂了」两类问题分开。"""
    problems = []
    if not os.path.isfile(paths.env_python):
        problems.append("InfiniteTalk 专用解释器不存在：{}".format(paths.env_python))
    if not os.path.isfile(paths.resolved_generate_script()):
        problems.append("找不到 generate_infinitetalk.py：{}".format(
            paths.resolved_generate_script()))
    for label, target in (("底模目录", paths.ckpt_dir),
                          ("wav2vec 目录", paths.wav2vec_dir),
                          ("量化权重目录", paths.quant_dir)):
        if not os.path.isdir(target):
            problems.append("{}不存在：{}".format(label, target))

    quant_file = os.path.join(paths.quant_dir, "infinitetalk_single_fp8.safetensors")
    if not os.path.isfile(quant_file):
        problems.append("缺量化权重（fp8 档位必需）：{}".format(quant_file))
    else:
        size_gb = os.path.getsize(quant_file) / 2 ** 30
        if size_gb < 10:
            problems.append("量化权重体积异常（{:.2f} GB），可能没下完：{}".format(
                size_gb, quant_file))
    return problems


def gpu_memory_used():
    """当前显存占用（MiB）；拿不到就返回 None。"""
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10)
        return int(out.stdout.strip().splitlines()[0])
    except Exception:
        return None


def main():
    parser = argparse.ArgumentParser(description="InfiniteTalk 单页冒烟测试")
    parser.add_argument("--idx", type=int, default=0, help="用第几页的音频（0 起）")
    parser.add_argument("--profile", type=str, default=None, help="运行档位名")
    parser.add_argument("--ref_img", type=str, default=DEFAULT_REF_IMG)
    parser.add_argument("--result_dir", type=str, default=DEFAULT_RESULT)
    parser.add_argument("--save_dir", type=str, default=None,
                        help="产物目录；默认写到 result_dir/talking_smoke")
    parser.add_argument("--list-profiles", action="store_true")
    args = parser.parse_args()

    if args.list_profiles:
        print(list_profiles())
        return 0

    profile = resolve_profile(args.profile)
    paths = resolve_paths()

    audio_path = os.path.join(args.result_dir, "audio", "{}.wav".format(args.idx))
    save_dir = args.save_dir or os.path.join(args.result_dir, "talking_smoke")

    print("=" * 74)
    print("  InfiniteTalk 单页冒烟测试")
    print("=" * 74)
    print("  档位      : {}".format(describe_profile(profile)))
    print("  参考图    : {}".format(os.path.abspath(args.ref_img)))
    print("  驱动音频  : {}".format(os.path.abspath(audio_path)))
    print("  产物目录  : {}".format(os.path.abspath(save_dir)))
    print()

    if not os.path.isfile(audio_path):
        print("找不到驱动音频。请先跑完主链路的 stage 2：")
        print("  python pipeline_light.py --stage '[\"2\"]' ...")
        return 2

    problems = check_prerequisites(paths)
    if problems:
        print("前置检查未通过：")
        for item in problems:
            print("  - {}".format(item))
        print()
        print("权重下载：python scripts/download_models.py --group phase3 --source huggingface")
        return 2
    print("前置检查：通过")
    print()

    before = gpu_memory_used()
    if before is not None:
        print("起始显存占用：{} MiB".format(before))

    task = (args.idx, args.ref_img, audio_path, save_dir, 0,
            paths.env_python, profile)
    start = time.time()
    idx, ok, info = run_infinitetalk_inference(task)
    elapsed = time.time() - start

    after = gpu_memory_used()
    print()
    print("=" * 74)
    print("  结果：{}".format("成功" if ok else "失败"))
    print("  单页耗时：{:.1f}s （{:.1f} 分钟）".format(elapsed, elapsed / 60))
    if after is not None:
        print("  结束显存占用：{} MiB".format(after))
    print("  产物：{}".format(info))
    if not ok:
        return 1

    # 用 ffprobe 确认产物真的是可用的视频（有画面、有音轨），
    # 而不是一个 0 字节或无声的文件 —— 后续合成会跳过无音轨的片段。
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries",
         "format=duration", "-show_entries",
         "stream=codec_type,width,height",
         "-of", "default=noprint_wrappers=1", info],
        capture_output=True, text=True)
    print("  ffprobe：")
    for line in probe.stdout.strip().splitlines():
        print("      {}".format(line))
    return 0


if __name__ == "__main__":
    sys.exit(main())
