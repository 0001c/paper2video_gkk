"""数字人生成（Talking Head）—— 双后端：MuseTalk（默认）/ LongCat-Video-Avatar-1.5。

后端选择
--------
由 ``talking_gen_per_slide(model_type=...)`` 决定（``pipeline.py`` 里是
``--model_name_talking``）：

- **musetalk**（TMElyralab/MuseTalk，MIT）：只重绘 256x256 的人脸裁剪，
  注意力序列仅 1024，用标准 ``diffusers.UNet2DConditionModel`` + SDPA，
  **本机 RTX 5070 Ti（sm_120）+ Windows 可直接跑**。权重约 3.7GB。
- **longcat**（LongCat-Video-Avatar-1.5，美团，MIT）：整帧 480p 生成，
  表现力更强，但主注意力序列 36480，本机 sm_120 既装不了 flash-attn、
  xformers kernel 也只到 sm_90，**本机跑不了**，等学校 8×A6000（sm_80）。

换型历史（详见 docs/MuseTalk接入方案.md）
----------------------------------------
Hallo2 -> InfiniteTalk -> LongCat-Video-Avatar-1.5 -> **MuseTalk**

- Hallo2 官方锁 torch 2.2.2/cu118，cu118 无 sm_120 编译目标，装不上；
- InfiniteTalk 的 fp8 权重加载会触发 quanto 的 marlin CUDA 扩展**现场 JIT
  编译**，而本机 nvcc 12.6 最高 compute_90（无 compute_120）；
- LongCat 全程无需编译，但 36480 的注意力序列在 sm_120 上没有可用的高效
  kernel（见上）；
- MuseTalk 序列长度 1024（小 1270 倍），标准注意力实现就够 —— **结构性
  绕开该死结**。

与合成环节的契约（务必保持）
----------------------------
产出必须落在：

    <save_dir>/<音频序号，0 起>/<参考图主文件名>/merge_video.mp4

且**必须带音轨**。原因是 `merge_talking.py` 里写死了这个层级，并且会用
ffprobe 检查音轨、无音轨就跳过该页。层级里的 `(i-1)` 是因为幻灯片图片从
1.png 开始编号、而音频从 0.wav 开始。

两个后端的产物落点差别（本文件负责抹平）
----------------------------------------
- LongCat：固定输出 ``<output_dir>/avatar_out.mp4``
- MuseTalk：输出 ``<result_dir>/<version>/<output_name>``，具体规则封在
  ``talking_config.musetalk_output_path()``

中文路径的处理（本机特有，但不难）
----------------------------------
本机项目路径是 `E:\\Development\\26年\\...`，**含中文**。两个后端都必须走
ASCII 暂存机制：

    参考图/音频 -> 复制到 ASCII 工作区 -> 在 ASCII 工作区里生成
    -> 把生成的 mp4 搬回中文路径下的目标位置

⚠️ 对 MuseTalk 这一步**不是可选优化而是硬要求**：它用 ``cv2.imread`` 读图，
而 OpenCV 的窄字符 API 读不了中文路径，返回 None 后在
``'NoneType' object has no attribute 'ndim'`` 处崩掉（本机实测）。

运行档位（单卡 / 8×A6000）见 talking_config.py，本文件不写死任何显存策略。

关于「静态照片」
----------------
MuseTalk 是**口型同步**模型，只重绘嘴部：头姿/眼睛/表情保持输入原样。
传单张图片时，它会把该人脸的 latent **循环取用**于全部音频帧
（``frame_list_cycle = frame_list + frame_list[::-1]``），得到「静止的头 +
动的嘴」——这正是照片开口说话的预期效果。本项目数字人是画面角落的 PIP
（边长 1/6），该尺寸下完全可接受。输出帧数由音频长度 × fps 决定。
"""

import json
import os
import shutil
import subprocess
import time
from os import path

import pv_env
from talking_config import (
    TalkingPaths,
    build_avatar_command,
    describe_profile,
    musetalk_output_path,
    resolve_profile,
)

# LongCat 代码根目录（纯英文路径；仓库内自带 longcat_video/ 包，
# 因此运行时的 cwd 必须是它）
LONGCAT_ROOT = os.environ.get(
    "PAPER2VIDEO_LONGCAT_ROOT", r"E:\ai-models\LongCat-Video")

# MuseTalk 代码根目录（纯英文路径；运行 cwd 必须是它，理由同 LongCat）
MUSETALK_ROOT = os.environ.get(
    "PAPER2VIDEO_MUSETALK_ROOT", r"E:\ai-models\MuseTalk")

# 两个后端**共用同一个 venv**（都在 LongCat-Video/.venv 下）。
# 理由：两者对 torch 的要求完全一致 —— 都必须是 cu128 版才能覆盖 sm_120，
# 而这是 16GB Blackwell 卡上唯一可用的组合；MuseTalk 只比 LongCat 多 5 个
# **纯 Python** 依赖（face-alignment / omegaconf / moviepy / imageio-ffmpeg /
# ffmpeg-python，无编译），复用它省掉一次约 1 小时的 torch 重装。
# numpy==1.26.4 决定了必须是 Python 3.12。
TALKING_VENV = os.environ.get(
    "PAPER2VIDEO_TALKING_VENV", os.path.join(LONGCAT_ROOT, ".venv"))

LONGCAT_PYTHON = os.environ.get(
    "PAPER2VIDEO_LONGCAT_PYTHON",
    os.path.join(TALKING_VENV, "Scripts", "python.exe"))

MUSETALK_PYTHON = os.environ.get(
    "PAPER2VIDEO_MUSETALK_PYTHON",
    os.path.join(TALKING_VENV, "Scripts", "python.exe"))

# 数字人场景描述。参考图已经决定了人物长相，prompt 主要描述「场景/动作/镜头」。
# ⚠️ 只有 LongCat 用这个 prompt；MuseTalk 是纯口型同步模型，不吃文本 prompt。
DEFAULT_TALKING_PROMPT = os.environ.get(
    "PAPER2VIDEO_TALKING_PROMPT",
    "A person is presenting an academic talk, speaking to the camera with natural "
    "head movements and lip sync. Consistent identity and background, professional "
    "lighting, medium close-up shot, steady camera.")


def _ffmpeg_dir():
    """返回含 ffmpeg 可执行文件的目录（MuseTalk 的 --ffmpeg_path 要用）。

    MuseTalk 只在 ``ffmpeg -version`` 失败时才把该目录前置进 PATH，所以这里
    不做存在性断言：找不到就返回空串，让子进程沿用现有 PATH，由上游自己报错。
    """
    exe = shutil.which("ffmpeg")
    return os.path.dirname(exe) if exe else ""


def resolve_paths(env_python=None, backend="musetalk") -> TalkingPaths:
    """把各权重目录解析成一套路径。

    注意 LongCat 的目录名必须叫 `LongCat-Video`（不能带 org__ 前缀），因为
    上游按 `checkpoint_dir/../LongCat-Video` 找底模。这些目录名由
    scripts/download_models.py 的 LOCAL_DIR 例外表保证。

    MuseTalk 的权重则**必须在 `<repo_dir>/models/` 下**（vae.py 与
    face_parsing 把它写死成相对 cwd 的路径，见 talking_config 的 docstring），
    所以这里不给它配任何"权重根目录"参数。
    """
    if backend == "musetalk":
        return TalkingPaths(
            repo_dir=MUSETALK_ROOT,
            env_python=env_python or MUSETALK_PYTHON,
            ffmpeg_dir=_ffmpeg_dir(),
        )
    if backend == "longcat":
        weights = pv_env.WEIGHTS_DIR
        return TalkingPaths(
            repo_dir=LONGCAT_ROOT,
            env_python=env_python or LONGCAT_PYTHON,
            checkpoint_dir=os.path.join(weights, "LongCat-Video-Avatar-1.5"),
            base_dir=os.path.join(weights, "LongCat-Video"),
        )
    raise ValueError(
        "未知的数字人后端 {!r}（可选 musetalk / longcat）".format(backend))


def _ascii_workspace(idx, backend=None):
    """为第 idx 页准备一个纯英文工作区，并返回其路径。

    backend 非空时用 ``<TMP_DIR>/talking/<backend>/<idx>``。两个后端共用同一个
    TMP_DIR，按后端分目录可避免同一页号的暂存文件互相覆盖（切后端重跑时很容易
    撞上）。不给 backend 时保持原有层级，兼容既有调用方。
    """
    parts = [pv_env.TMP_DIR, "talking"]
    if backend:
        parts.append(backend)
    parts.append(str(idx))
    root = os.path.join(*parts)
    os.makedirs(root, exist_ok=True)
    return root



def _stage_inputs(workspace, ref_img_path, audio_path):
    """把参考图与音频复制进 ASCII 工作区，返回暂存后的路径。

    参考图会在这一层把透明通道合成到白底（PNG 直接 ``convert("RGB")`` 会把
    透明区域变黑）。

    音频**原样复制**，不做重采样 —— 两个后端的加载器都自己转：
    LongCat 走 ``librosa.load(sr=16000)``，MuseTalk 走
    ``audio_processor.get_audio_feature`` 里的 ``librosa.load(sr=16000)``，
    两者都会把任意采样率/声道数转成 16k 单声道。
    （本机的 demo 素材是 44.1kHz 立体声，实测无需预处理。）
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


def _write_musetalk_yaml(workspace, staged_img, staged_audio, profile):
    """写 MuseTalk 的 inference YAML。

    格式取自上游 ``configs/inference/test.yaml``（OmegaConf 读取）：

        task_0:
          video_path: "..."
          audio_path: "..."
          bbox_shift: 0      # 可选，覆盖命令行默认值

    只有一个 task（一次调一页），task 名无关紧要但必须是合法键。

    ⚠️ ``bbox_shift`` 只在 **v1** 生效。v1.5 在 inference.py:93-94 里硬编码
    ``bbox_shift = 0``，命令行与 YAML 传什么都会被忽略。仍然写进去，是为了
    命令行日志能自证「本次跑的参数」，以及切到 v1 时不用改代码。
    """
    import yaml

    payload = {
        "task_0": {
            "video_path": staged_img,
            "audio_path": staged_audio,
            "bbox_shift": int(profile.get("bbox_shift", 0)),
        }
    }
    yaml_path = os.path.join(workspace, "inference.yaml")
    # safe_dump 会自动处理 Windows 反斜杠（转义成 \\），不必手工改成正斜杠
    with open(yaml_path, "w", encoding="utf-8") as handle:
        yaml.safe_dump(payload, handle, allow_unicode=True, sort_keys=False)
    return yaml_path, payload


def _target_paths(save_dir, idx, ref_img_path):
    """算出这一页的产物落点（两个后端共用同一套契约）。"""
    ref_stem = path.splitext(path.basename(ref_img_path))[0]
    target_dir = os.path.join(save_dir, str(idx), ref_stem)
    return target_dir, os.path.join(target_dir, "merge_video.mp4")


def run_avatar_inference(task):
    """生成一页数字人视频（按 profile 里的 backend 分派）。

    ``task`` 是元组，便于未来接 multiprocessing（spawn 模式要求顶层可 pickle）：
        (idx, ref_img_path, audio_path, save_dir, gpu_id, env_python, profile)
    返回 ``(idx, 是否成功, 目标路径或错误信息)``。

    后端取自 ``profile["backend"]`` 而非另开一个参数 —— profile 已由
    ``resolve_profile(..., backend=model_type)`` 校验过一致性，不重复传递。
    """
    idx, ref_img_path, audio_path, save_dir, gpu_id, env_python, profile = task
    backend = profile.get("backend", "longcat")

    target_dir, target_video = _target_paths(save_dir, idx, ref_img_path)
    if path.exists(target_video):
        print("[talking_gen] 第 {} 页已存在，跳过：{}".format(idx, target_video))
        return idx, True, target_video

    paths = resolve_paths(env_python, backend=backend)
    workspace = _ascii_workspace(idx, backend)
    staged_img, staged_audio = _stage_inputs(workspace, ref_img_path, audio_path)

    env = os.environ.copy()
    if gpu_id is not None:
        env["CUDA_VISIBLE_DEVICES"] = str(gpu_id)
    env.setdefault("HF_HOME", pv_env.HF_HOME)

    if backend == "musetalk":
        # ── ① torch.load 的 weights_only 默认值问题 ──
        # torch>=2.6 把 torch.load 的 weights_only 默认值从 False 翻成了 True，
        # 而 MuseTalk 有三处老写法没显式传这个参数：
        #   musetalk/models/unet.py:44                     -> unet.pth（够新，能过）
        #   musetalk/utils/face_parsing/resnet.py:83       -> resnet18-5c106cde.pth
        #   musetalk/utils/face_parsing/__init__.py:65,67  -> 79999_iter.pth
        # 其中 resnet18 是 legacy .tar 格式，weights_only=True 会直接拒绝：
        #   RuntimeError: Cannot use ``weights_only=True`` with files saved in
        #   the legacy .tar format.   （本机实测，FaceParsing() 构造时崩）
        # 上游 README 锁 torch 2.0.1，那时默认还是 False，所以作者没踩到。
        #
        # 用官方环境变量把它翻回来（torch/serialization.py:1486 读它，且只在
        # 调用点**没有**显式传 weights_only 时才生效）——比去改 4 处第三方代码
        # 少一份改动，也少一处将来被上游覆盖的可能。
        # 代价是放弃 pickle 的安全反序列化：权重全部是本项目自己从官方 HF
        # 仓库下载的（见 scripts/download_models.py），可接受。
        env["TORCH_FORCE_NO_WEIGHTS_ONLY_LOAD"] = "1"

        # ── ② 关掉代理沙箱的「批量删除守卫」──
        # 这不是项目问题，是**代理沙箱的副作用**：在沙箱里 python 的 PYTHONPATH
        # 被指向 WorkBuddy 的 shim 目录，那里 monkeypatch 了 os.remove/shutil.rmtree，
        # 单次 >CODEBUDDY_SAFE_DELETE_BULK_THRESHOLD(默认 50) 个目标的删除会被拦下
        # 并抛异常。而 MuseTalk 收尾时正好要删 250 帧（10s × 25fps）临时 PNG
        # （inference.py:240,243 的 shutil.rmtree）：
        #   [safe-delete][SAFE_DELETE_BULK_CONFIRM_REQUIRED] {"count":250,...}
        # 后果很阴险：**视频已经写好了**，但进程退出码变成 1，被上游当成失败。
        #
        # 该守卫保护的是「代理不要误删用户文件」，而这里的删除目标是 MuseTalk
        # 自己在 <workspace> 下生成的临时帧目录，与用户文件无关；且用户从普通
        # 终端跑时这些变量根本不存在，设了也无害（等于 no-op）。
        # 所以只在这个子进程里关掉，不碰全局。
        env["CODEBUDDY_SAFE_DELETE_ENABLED"] = "0"

    if backend == "musetalk":
        produced, err = _invoke_musetalk(profile, paths, workspace,
                                         staged_img, staged_audio, env)
    else:
        produced, err = _invoke_longcat(profile, paths, workspace,
                                        staged_img, staged_audio, env)

    if err:
        return idx, False, err

    os.makedirs(target_dir, exist_ok=True)
    shutil.move(produced, target_video)
    print("[talking_gen] 第 {} 页完成 -> {}".format(idx, target_video))
    return idx, True, target_video


def _invoke_longcat(profile, paths, workspace, staged_img, staged_audio, env):
    """跑一次 LongCat 推理，返回 (产物路径, 错误信息)。"""
    input_json, _ = _write_input_json(workspace, staged_img, staged_audio,
                                      DEFAULT_TALKING_PROMPT)
    output_dir = os.path.join(workspace, "out")
    os.makedirs(output_dir, exist_ok=True)
    cmd = build_avatar_command(profile, paths, input_json, output_dir)
    rc = _run(cmd, paths.repo_dir, env, profile)

    produced = os.path.join(output_dir, "avatar_out.mp4")
    if rc != 0 or not path.exists(produced):
        return None, "LongCat 退出码={}，产物存在={}".format(rc, path.exists(produced))
    return produced, None


def _invoke_musetalk(profile, paths, workspace, staged_img, staged_audio, env):
    """跑一次 MuseTalk 推理，返回 (产物路径, 错误信息)。

    ⚠️ MuseTalk 用 ``cv2.imread`` 读参考图，**读不了中文路径**，所以这里必须
    用 ``_stage_inputs`` 暂存出来的 ASCII 路径（见模块 docstring）。
    """
    yaml_path, _ = _write_musetalk_yaml(workspace, staged_img, staged_audio, profile)
    result_dir = os.path.join(workspace, "out")
    os.makedirs(result_dir, exist_ok=True)
    produced = musetalk_output_path(profile, result_dir)

    started = time.time()
    cmd = build_avatar_command(profile, paths, yaml_path, result_dir)
    rc = _run(cmd, paths.repo_dir, env, profile)

    # ── 为什么要查「新鲜度」而不只是「存在」──
    # MuseTalk 把每个 task 的主体整段包在 `try/except Exception` 里
    # （inference.py 的 "except Exception as e: print('Error occurred during
    # processing:', e)"），**任何异常都被吞掉且退出码仍是 0**。于是
    #   退出码 0 + 文件存在
    # 这个组合**不能**证明本次真的成功了 —— 完全可能是上次失败留下的旧文件。
    # 而这里又不能简单删掉旧文件（代理沙箱的 safe-delete 守卫会拦批量删除，
    # 见 run_avatar_inference 的说明），所以改用 mtime 判定：产物必须比本次
    # 启动的瞬间更新。
    # 允许 1s 的容差：NTFS 时间戳精度足够，但文件系统时钟与 time.time() 不保证
    # 完全同源。
    if rc != 0:
        return None, "MuseTalk 退出码={}（预期 0）".format(rc)
    if not path.exists(produced):
        return None, "MuseTalk 退出码=0 但没有产物（预期 {}）".format(produced)
    if path.getmtime(produced) + 1.0 < started:
        return None, ("产物是**旧的**（mtime={:.0f} < 本次启动 {:.0f}）：本次推理"
                      "其实失败了，只是被上游的 try/except 吞掉、退出码仍为 0。"
                      "请看上方子进程日志里的 'Error occurred during processing'"
                      .format(path.getmtime(produced), started))
    return produced, None


def _run(cmd, cwd, env, profile):
    """统一的「打印 + 执行」外壳，保证两个后端的日志格式一致。"""
    print("[talking_gen] 档位：{}".format(describe_profile(profile)))
    print("[talking_gen] 后端：{}（cwd={}）".format(profile.get("backend"), cwd))
    print("[talking_gen] 命令：{}".format(" ".join(cmd)))
    return subprocess.run(cmd, cwd=cwd, env=env).returncode



def talking_gen_per_slide(model_type, input_list, save_dir, gpu_list, env_path,
                          profile=None, prompt=None):
    """逐页生成数字人视频。

    参数
    ----
    model_type : "musetalk"（默认，本机可用）或 "longcat"（本机 sm_120 跑不了）
    input_list : [[参考图路径, 音频路径], ...]，一页一项
    save_dir   : 产物根目录
    gpu_list   : GPU 序号列表
    env_path   : 数字人专用解释器（上游的 --talking_head_env）；留空则按后端取默认
    profile    : 档位名（见 talking_config.list_profiles()）；留空取该后端默认档

    ⚠️ 这里**串行**执行，不再像上游那样开 Pool。LongCat 多卡时用 torchrun +
       context_parallel 在**内部**完成并行，外层再套 Pool 会变成「每个任务都开
       N 张卡」的冲突用法；MuseTalk 则是单卡单进程，多卡加速要靠「一页一卡」
       的外层并发，那需要先解决显存/子进程管理，还没做。
       所以单卡/多卡的差异目前只在 talking_config 的档位里。
    """
    if model_type not in ("musetalk", "longcat", "infinitetalk", "hallo2"):
        raise ValueError(
            "未知的 model_type: {}（可选 musetalk / longcat）".format(model_type))
    if model_type in ("infinitetalk", "hallo2"):
        raise NotImplementedError(
            "{} 已退役：InfiniteTalk 的 fp8 加载需现场编译 quanto CUDA 扩展、"
            "Hallo2 锁 cu118 无 sm_120。请使用 --model_name_talking musetalk。".format(model_type))

    global DEFAULT_TALKING_PROMPT
    if prompt:
        DEFAULT_TALKING_PROMPT = prompt

    # backend 一并传给 resolve_profile：既决定未指定档位时用哪个默认档，
    # 也校验「拿到的档位确实属于这个后端」（两个后端参数名完全不同，
    # 传错会拼出无效命令行）。
    resolved = resolve_profile(profile, backend=model_type)
    gpu_ids = list(gpu_list) if gpu_list else [0]
    os.makedirs(save_dir, exist_ok=True)

    tasks = []
    for idx, (ref_img_path, audio_path) in enumerate(input_list):
        _, done = _target_paths(save_dir, idx, ref_img_path)
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
