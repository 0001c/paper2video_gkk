"""数字人生成（Talking Head）—— LongCat-Video-Avatar-1.5 后端。

替换说明（2026-09-17，详见 docs/模型替换方案.md）
------------------------------------------------
Hallo2 -> InfiniteTalk -> **LongCat-Video-Avatar-1.5（美团，MIT）**

换型原因（简述）：
- Hallo2 官方锁 torch 2.2.2/cu118，cu118 无 sm_120 编译目标，RTX 5070 Ti 装不上；
- InfiniteTalk 的 fp8 权重加载会触发 quanto 的 marlin CUDA 扩展**现场 JIT 编译**，
  而本机 nvcc 12.6 最高 compute_90（无 compute_120），只能编 9.0+PTX 且与
  quanto 的 -DQUANTO_CUDA_ARCH=1200 宏是否冲突无法验证；
- LongCat-Video-Avatar-1.5 是 diffusers 生态、自带 int8 量化 + DMD2 蒸馏（8 步）、
  音频编码器为 Whisper-large-v3，注意力后端可用 xformers（官方 win wheel），
  **全程无需现场编译任何扩展**。

与合成环节的契约（务必保持）
----------------------------
产出必须落在：

    <save_dir>/<音频序号，0 起>/<参考图主文件名>/merge_video.mp4

且**必须带音轨**。原因是 `merge_talking.py` 里写死了这个层级，并且会用
ffprobe 检查音轨、无音轨就跳过该页。层级里的 `(i-1)` 是因为幻灯片图片从
1.png 开始编号、而音频从 0.wav 开始。

中文路径的处理（本机特有，但不难）
----------------------------------
本机项目路径是 `E:\\Development\\26年\\...`，**含中文**。稳妥起见沿用 ASCII
暂存机制（虽然 LongCat 不像 InfiniteTalk 那样明确要求纯英文路径，但 torch/
ffmpeg/librosa 的窄字符 API 在不同环节读中文路径都可能翻车，统一隔离更稳）：

    参考图/音频 -> 复制到 ASCII 工作区 -> 在 ASCII 工作区里生成
    -> 把生成的 mp4 搬回中文路径下的目标位置

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
    build_avatar_command,
    describe_profile,
    resolve_profile,
)

# LongCat 代码根目录（纯英文路径；仓库内自带 longcat_video/ 包，
# 因此运行时的 cwd 必须是它）
LONGCAT_ROOT = os.environ.get(
    "PAPER2VIDEO_LONGCAT_ROOT", r"E:\ai-models\LongCat-Video")

# 专用 venv（对应上游的 --talking_head_env；numpy==1.26.4 决定了必须用 Python 3.12）
LONGCAT_PYTHON = os.environ.get(
    "PAPER2VIDEO_LONGCAT_PYTHON",
    os.path.join(LONGCAT_ROOT, ".venv", "Scripts", "python.exe"))

# 数字人场景描述。参考图已经决定了人物长相，prompt 主要描述「场景/动作/镜头」。
DEFAULT_TALKING_PROMPT = os.environ.get(
    "PAPER2VIDEO_TALKING_PROMPT",
    "A person is presenting an academic talk, speaking to the camera with natural "
    "head movements and lip sync. Consistent identity and background, professional "
    "lighting, medium close-up shot, steady camera.")


def resolve_paths(env_python=None) -> TalkingPaths:
    """把各权重目录解析成一套路径。

    注意 LongCat 的目录名必须叫 `LongCat-Video`（不能带 org__ 前缀），因为
    上游按 `checkpoint_dir/../LongCat-Video` 找底模。这些目录名由
    scripts/download_models.py 的 LOCAL_DIR 例外表保证。
    """
    weights = pv_env.WEIGHTS_DIR
    return TalkingPaths(
        repo_dir=LONGCAT_ROOT,
        env_python=env_python or LONGCAT_PYTHON,
        checkpoint_dir=os.path.join(weights, "LongCat-Video-Avatar-1.5"),
        base_dir=os.path.join(weights, "LongCat-Video"),
    )


def _ascii_workspace(idx):
    """为第 idx 页准备一个纯英文工作区，并返回其路径。"""
    root = os.path.join(pv_env.TMP_DIR, "talking", str(idx))
    os.makedirs(root, exist_ok=True)
    return root


def _stage_inputs(workspace, ref_img_path, audio_path):
    """把参考图与音频复制进 ASCII 工作区，返回暂存后的路径。

    参考图会在这一层把透明通道合成到白底（PNG 直接 convert("RGB") 会把透明
    区域变黑），并把音频统一转成 16k 单声道 wav（LongCat 的音频编码器与
    librosa.load(sr=16000) 都按这个约定）。
    """
    from PIL import Image

    staged_img = os.path.join(workspace, "ref_image.png")
    with Image.open(ref_img_path) as handle:
        if handle.mode in ("RGBA", "LA", "P"):
            rgba = handle.convert("RGBA")
            flat = Image.new("RGB", rgba.size, (255, 255, 255))
            flat.paste(rgba, mask=rgba.split()[-1])
            image = flat
        else:
            image = handle.convert("RGB")
        image.save(staged_img)

    staged_audio = os.path.join(workspace, "driving_audio" +
                                path.splitext(audio_path)[1])
    shutil.copyfile(audio_path, staged_audio)
    return staged_img, staged_audio


def _write_input_json(workspace, staged_img, staged_audio, prompt):
    """写 LongCat 的输入描述文件（与上游 demo 的 JSON 约定一致）。"""
    payload = {
        "prompt": prompt,
        "cond_image": staged_img,
        "cond_audio": {"person1": staged_audio},
    }
    json_path = os.path.join(workspace, "input.json")
    with open(json_path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False)
    return json_path, payload


def run_avatar_inference(task):
    """生成一页数字人视频。

    ``task`` 是元组，便于未来接 multiprocessing（spawn 模式要求顶层可 pickle）：
        (idx, ref_img_path, audio_path, save_dir, gpu_id, env_python, profile)
    返回 ``(idx, 是否成功, 目标路径或错误信息)``。
    """
    idx, ref_img_path, audio_path, save_dir, gpu_id, env_python, profile = task

    ref_stem = path.splitext(path.basename(ref_img_path))[0]
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

    output_dir = os.path.join(workspace, "out")
    os.makedirs(output_dir, exist_ok=True)
    cmd = build_avatar_command(profile, paths, input_json, output_dir)

    env = os.environ.copy()
    if gpu_id is not None:
        env["CUDA_VISIBLE_DEVICES"] = str(gpu_id)
    env.setdefault("HF_HOME", pv_env.HF_HOME)

    print("[talking_gen] 档位：{}".format(describe_profile(profile)))
    print("[talking_gen] 第 {} 页开始生成（cwd={}）".format(idx, paths.repo_dir))
    print("[talking_gen] 命令：{}".format(" ".join(cmd)))

    result = subprocess.run(cmd, cwd=paths.repo_dir, env=env)

    # 推理脚本固定输出 avatar_out.mp4，并在 stdout 末尾打一行 AVATAR_OUTPUT=<path>
    produced = os.path.join(output_dir, "avatar_out.mp4")
    if result.returncode != 0 or not path.exists(produced):
        return idx, False, "LongCat 退出码={}，产物存在={}".format(
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
    model_type : "longcat"（新）或 "infinitetalk"/"hallo2"（旧，本机不可用）
    input_list : [[参考图路径, 音频路径], ...]，一页一项
    save_dir   : 产物根目录
    gpu_list   : GPU 序号列表
    env_path   : 数字人专用解释器（上游的 --talking_head_env）

    ⚠️ 这里**串行**执行，不再像上游那样开 Pool。多卡时 LongCat 用 torchrun +
       context_parallel 在**内部**完成并行，外层再套 Pool 会变成「每个任务都开
       N 张卡」的冲突用法。所以单卡/多卡的差异只在 talking_config 的档位里。
    """
    if model_type not in ("longcat", "infinitetalk", "hallo2"):
        raise ValueError("未知的 model_type: {}（可选 longcat / infinitetalk / hallo2）".format(model_type))
    if model_type in ("infinitetalk", "hallo2"):
        raise NotImplementedError(
            "{} 已退役：InfiniteTalk 的 fp8 加载需现场编译 quanto CUDA 扩展、"
            "Hallo2 锁 cu118 无 sm_120。请使用 --model_name_talking longcat。".format(model_type))

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
        results.append(run_avatar_inference(task))

    failures = [r for r in results if not r[1]]
    if failures:
        print("[talking_gen] 有 {} 页失败：".format(len(failures)))
        for idx, _, info in failures:
            print("  第 {} 页：{}".format(idx, info))
    return results
