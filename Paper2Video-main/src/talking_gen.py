"""数字人生成（Talking Head）—— InfiniteTalk 后端。

替换说明（2026-09，详见 docs/模型替换方案.md）
----------------------------------------------
Hallo2 -> MeiGen-AI/InfiniteTalk
    1. **Hallo2 在本机根本装不上**：官方锁 torch 2.2.2 / cu118，而 cu118 没有
       sm_120 编译目标，RTX 5070 Ti(Blackwell) 装上即报算力不支持。
       InfiniteTalk 以 Wan2.1-I2V-14B-480P 为基座，官方要求 CUDA>=12.8，正好解掉。
    2. 时长：Hallo2 单次生成有硬上限；InfiniteTalk 用 streaming + 时序上下文帧，
       可无限长。我们每页音频 14~20s，必须靠它。
    3. 许可证：Apache-2.0。

与合成环节的契约（务必保持）
----------------------------
产出必须落在：

    <save_dir>/<音频序号，0 起>/<参考图主文件名>/merge_video.mp4

且**必须带音轨**。原因是 `1_merage.bash` / `merge_talking.py` 里写死了这个层级，
并且会用 ffprobe 检查音轨、无音轨就跳过该页。层级里的 `(i-1)` 是因为幻灯片图片
从 1.png 开始编号、而音频从 0.wav 开始。

中文路径的处理（本机特有，但不难）
----------------------------------
本机项目路径是 `E:\\Development\\26年\\...`，**含中文**；而 InfiniteTalk 明确要求
纯英文路径（其依赖的 cv2 / ffmpeg / 各种 C 扩展在窄字符 API 上会读不到文件）。
因此这里不直接把中文路径喂给它，而是：

    参考图/音频 -> 复制到 ASCII 工作区 -> 在 ASCII 工作区里生成
    -> 把生成的 mp4 搬回中文路径下的目标位置

对 InfiniteTalk 来说它只见到纯英文路径，对上层则完全透明。

运行档位（单卡 / 8×A6000）见 talking_config.py，本文件不写死任何显存策略。
"""

import json
import os
import shutil
import subprocess
from os import path

import pv_env
from talking_config import (
    TalkingPaths,
    build_infinitetalk_command,
    describe_profile,
    resolve_profile,
)

# InfiniteTalk 代码根目录（纯英文路径；仓库内自带 wan/、src/、kokoro/，
# 因此运行时的 cwd 必须是它）
INFINITETALK_ROOT = os.environ.get(
    "PAPER2VIDEO_INFINITETALK_ROOT", r"E:\ai-models\InfiniteTalk")

# 专用 venv（对应上游的 --talking_head_env；numpy<2 决定了必须用 Python 3.12）
INFINITETALK_PYTHON = os.environ.get(
    "PAPER2VIDEO_INFINITETALK_PYTHON",
    os.path.join(INFINITETALK_ROOT, ".venv", "Scripts", "python.exe"))

# 数字人场景描述。参考图已经决定了人物长相，prompt 主要描述「场景/动作/镜头」。
# InfiniteTalk 的 prompt 用英文效果最好；可按需用环境变量覆盖。
DEFAULT_TALKING_PROMPT = os.environ.get(
    "PAPER2VIDEO_TALKING_PROMPT",
    "A person is presenting an academic talk, speaking to the camera with natural "
    "head movements and lip sync. Consistent identity and background, professional "
    "lighting, medium close-up shot, steady camera.")


def resolve_paths(env_python=None) -> TalkingPaths:
    """把各权重目录解析成一套路径。

    权重目录全部走 pv_env 的 `org__name` 约定（与主链路一致），
    这样换机器时只要 PAPER2VIDEO_WEIGHTS 指向别处即可。
    """
    weights = pv_env.WEIGHTS_DIR
    return TalkingPaths(
        repo_dir=INFINITETALK_ROOT,
        env_python=env_python or INFINITETALK_PYTHON,
        ckpt_dir=os.path.join(weights, "Wan-AI__Wan2.1-I2V-14B-480P"),
        wav2vec_dir=os.path.join(weights, "TencentGameMate__chinese-wav2vec2-base"),
        quant_dir=os.path.join(weights, "MeiGen-AI__InfiniteTalk", "quant_models"),
        infinitetalk_dir=os.path.join(weights, "MeiGen-AI__InfiniteTalk", "single"),
    )


def _ascii_workspace(idx):
    """为第 idx 页准备一个纯英文工作区，并返回其路径。

    落在 pv_env.TMP_DIR（本机是 E:\\ai-models\\tmp）—— 整条路径不含中文，
    这是 InfiniteTalk 能正常读文件的前提。
    """
    root = os.path.join(pv_env.TMP_DIR, "talking", str(idx))
    os.makedirs(root, exist_ok=True)
    return root


def _stage_inputs(workspace, ref_img_path, audio_path):
    """把参考图与音频复制进 ASCII 工作区，返回复制后的路径。"""
    staged_img = os.path.join(workspace, "ref_image" + path.splitext(ref_img_path)[1])
    staged_audio = os.path.join(workspace, "driving_audio" +
                                path.splitext(audio_path)[1])
    shutil.copyfile(ref_img_path, staged_img)
    shutil.copyfile(audio_path, staged_audio)
    return staged_img, staged_audio


def _write_input_json(workspace, staged_img, staged_audio, prompt):
    """写 InfiniteTalk 的输入描述文件（单人的 cond_audio 键固定为 person1）。"""
    payload = {
        "prompt": prompt,
        "cond_video": staged_img,
        "cond_audio": {"person1": staged_audio},
    }
    json_path = os.path.join(workspace, "input.json")
    with open(json_path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False)
    return json_path, payload


def run_infinitetalk_inference(task):
    """生成一页数字人视频。

    ``task`` 是元组，便于未来接 multiprocessing（spawn 模式要求顶层可 pickle）：
        (idx, ref_img_path, audio_path, save_dir, gpu_id, env_python, profile)
    返回 ``(idx, 是否成功, 目标路径或错误信息)``。
    """
    idx, ref_img_path, audio_path, save_dir, gpu_id, env_python, profile = task

    ref_stem = path.splitext(path.basename(ref_img_path))[0]
    # 目标层级必须与 1_merage.bash 一致：<save_dir>/<idx>/<ref_stem>/merge_video.mp4
    target_dir = os.path.join(save_dir, str(idx), ref_stem)
    target_video = os.path.join(target_dir, "merge_video.mp4")

    if path.exists(target_video):
        print("[talking_gen] 第 {} 页已存在，跳过：{}".format(idx, target_video))
        return idx, True, target_video

    paths = resolve_paths(env_python)
    workspace = _ascii_workspace(idx)
    staged_img, staged_audio = _stage_inputs(workspace, ref_img_path, audio_path)
    input_json, _ = _write_input_json(workspace, staged_img, staged_audio,
                                     DEFAULT_TALKING_PROMPT)

    # save_file 给的是前缀，InfiniteTalk 会自己在后面补 .mp4
    save_prefix = os.path.join(workspace, "out")
    cmd = build_infinitetalk_command(profile, paths, input_json, save_prefix)

    env = os.environ.copy()
    if gpu_id is not None:
        env["CUDA_VISIBLE_DEVICES"] = str(gpu_id)
    # 让 InfiniteTalk 内部也复用主链路的缓存位置，避免写满 C 盘
    env.setdefault("HF_HOME", pv_env.HF_HOME)

    print("[talking_gen] 档位：{}".format(describe_profile(profile)))
    print("[talking_gen] 第 {} 页开始生成（cwd={}）".format(idx, paths.repo_dir))
    print("[talking_gen] 命令：{}".format(" ".join(cmd)))

    result = subprocess.run(cmd, cwd=paths.repo_dir, env=env)
    produced = save_prefix + ".mp4"
    if result.returncode != 0 or not path.exists(produced):
        return idx, False, "InfiniteTalk 退出码={}，产物存在={}".format(
            result.returncode, path.exists(produced))

    os.makedirs(target_dir, exist_ok=True)
    shutil.move(produced, target_video)
    print("[talking_gen] 第 {} 页完成 -> {}".format(idx, target_video))
    return idx, True, target_video


def talking_gen_per_slide(model_type, input_list, save_dir, gpu_list, env_path,
                          profile=None, prompt=None):
    """逐页生成数字人视频。

    参数
    ----
    model_type : "infinitetalk"（新）或 "hallo2"（旧，本机不可用）
    input_list : [[参考图路径, 音频路径], ...]，一页一项
    save_dir   : 产物根目录
    gpu_list   : GPU 序号列表
    env_path   : InfiniteTalk 专用解释器（上游的 --talking_head_env）

    ⚠️ 这里是**串行**执行的，不再像上游那样开 Pool。原因是 InfiniteTalk 的并行
       是在自己内部完成的：多卡时它用 torchrun 起 N 个 rank（FSDP + Ulysses），
       外层再套一层 Pool 会变成「每个任务都开 N 张卡」的冲突用法。
       所以单卡/多卡的差异只在 talking_config 的档位里，不在这里。
    """
    if model_type not in ("infinitetalk", "hallo2"):
        raise ValueError("未知的 model_type: {}（可选 infinitetalk / hallo2）".format(model_type))
    if model_type == "hallo2":
        raise NotImplementedError(
            "hallo2 在本机不可用：官方锁 torch 2.2.2/cu118，而 cu118 无 sm_120 "
            "编译目标，RTX 5070 Ti 无法运行。请使用 --model_name_talking infinitetalk。")

    global DEFAULT_TALKING_PROMPT
    if prompt:
        DEFAULT_TALKING_PROMPT = prompt

    resolved = resolve_profile(profile)
    gpu_ids = list(gpu_list) if gpu_list else [0]
    os.makedirs(save_dir, exist_ok=True)

    tasks = []
    for idx, (ref_img_path, audio_path) in enumerate(input_list):
        ref_stem = path.splitext(path.basename(ref_img_path))[0]
        done = path.join(save_dir, str(idx), ref_stem, "merge_video.mp4")
        if path.exists(done):
            print("[talking_gen] 第 {} 页已存在，跳过".format(idx))
            continue
        tasks.append((idx, ref_img_path, audio_path, save_dir,
                      gpu_ids[idx % len(gpu_ids)], env_path, resolved))

    if not tasks:
        print("[talking_gen] 没有需要生成的页（全部已存在）")
        return []

    print("[talking_gen] 待生成 {} 页，档位：{}".format(len(tasks), describe_profile(resolved)))
    results = []
    for task in tasks:
        results.append(run_infinitetalk_inference(task))

    failures = [r for r in results if not r[1]]
    if failures:
        print("[talking_gen] 有 {} 页失败：".format(len(failures)))
        for idx, _, info in failures:
            print("  第 {} 页：{}".format(idx, info))
    return results
