"""数字人（talking head）运行档位 —— 面向「换机器只改配置」的移植接口层。

支持两个后端
------------
1. **musetalk**（默认，2026-09-17 接入）：TMElyralab/MuseTalk，MIT。
   只重绘 256x256 人脸区域，注意力序列仅 1024，用标准 diffusers
   UNet2DConditionModel，SDPA 即可 —— 在 Blackwell(sm_120)/Windows 上
   **结构性绕开** flash-attn / xformers 的编译死结。详见 docs/MuseTalk接入方案.md
2. **longcat**：LongCat-Video-Avatar-1.5（美团，MIT）。整帧生成、表现力更强，
   但主注意力序列 36480，本机 sm_120 上跑不了。保留给 8×A6000（sm_80）档位。

每个档位都带一个 ``backend`` 字段，由 :func:`resolve_profile` 校验 ——
防止把 longcat 的档位误传给 musetalk 后端（两者参数名完全不同）。

为什么需要这一层
----------------
本机是 **RTX 5070 Ti 16GB（sm_120 / Windows）**，后续要迁到学校的
**8×A6000 48GB 服务器**。两边的差别是多维的：分辨率、量化方式、显存策略、
并行方式、权重根目录。收敛成 profile 之后，**迁移只改一个字符串**。

本层的职责边界（务必看清）
--------------------------
**只做「profile → 命令行参数」的翻译，不实现、也不验证真实的多卡并行。**
`server` 系档位是按各家官方多卡用法填写的，属于**预留接口**。真上机时若发现
字段有出入，只改本文件 `PROFILES` 字典即可，`talking_gen.py` 与 `pipeline.py`
都不用动。

切换档位的三种方式（优先级从高到低）
------------------------------------
1. `talking_gen_per_slide(..., profile="musetalk-local")` 显式传参
2. 环境变量 `PAPER2VIDEO_TALKING_PROFILE=musetalk-local`（写在 `.env` 里）
3. 按 `model_type` 取 `DEFAULT_PROFILE_BY_BACKEND` 里的默认档

⚠️ MuseTalk 的隐藏约束：它把两个路径**写死在代码里**，且都是相对 cwd 的：
- `musetalk/models/vae.py:33`   -> `VAE(model_path="models/" + vae_type)`
- `musetalk/utils/face_parsing/__init__.py` -> `"./models/face-parse-bisent/..."`

所以 `TalkingPaths.repo_dir` 必须等于 MuseTalk 仓库根，且权重必须落在
`<repo_dir>/models/` 下 —— 这也是 `download_models.py` 里把
`TMElyralab/MuseTalk` 的 LOCAL_DIR 例外交为 `""`（落到 models 根）的原因。
unet/whisper 两项倒是可以传绝对路径，本层会显式传给 inference.py。
"""

import os
from dataclasses import dataclass
from typing import List, Optional

DEFAULT_PROFILE = "musetalk-local"

# 每个后端的默认档位。talking_gen_per_slide 按 model_type 取用，
# 这样 `--model_name_talking musetalk` 不写 profile 也能落到对的档位。
DEFAULT_PROFILE_BY_BACKEND = {
    "musetalk": "musetalk-local",
    "longcat": "local-16g",
}

# ──────────────────────────────────────────────────────────────
# 档位定义
# ──────────────────────────────────────────────────────────────
# 键的归属（两个后端的参数名完全不同，故按 backend 区分）：
#   longcat : resolution / use_int8 / use_distill / num_inference_steps /
#             text_guidance_scale / audio_guidance_scale / num_frames
#   musetalk: version / fps / bbox_shift / extra_margin / batch_size /
#             parsing_mode / left_cheek_width / right_cheek_width / use_float16
# 公共键：backend / desc / parallel
PROFILES = {
    # ── MuseTalk（默认后端）──────────────────────────────────
    # 本机实测：unet 3.17GB + vae 0.31 + whisper 0.14 + face-parse 0.09
    # = 约 3.7GB，16GB 卡余量很大；--use_float16 进一步省一半。
    "musetalk-local": {
        "backend": "musetalk",
        "desc": "本机 RTX 5070 Ti 16GB 单卡（Windows / sm_120）",
        "version": "v15",
        "fps": 25,               # 与训练一致；输出帧数 = 音频时长 × fps
        # ⚠️ v1.5 在 inference.py:93-94 硬编码 bbox_shift=0，这里填什么都会被忽略。
        #    该旋钮只在 --version v1 下有效（-16~16，用于修口型裁剪偏上/偏下）。
        "bbox_shift": 0,
        "extra_margin": 10,      # 人脸裁剪外扩，给下巴留余量
        "batch_size": 8,         # UNet 前向批大小（256x256，8 很轻松）
        "parsing_mode": "jaw",   # v1.5 的默认融合模式
        "left_cheek_width": 90,
        "right_cheek_width": 90,
        "use_float16": True,     # fp16 推理，省显存也更快
        "parallel": {"mode": "single"},
    },

    # 学校服务器：8×A6000 48GB
    # ⚠️ 预留档位，未实机验证。MuseTalk 是单卡单进程实现（device=cuda:{gpu_id}），
    #    没有 torchrun/context_parallel 之类的内部并行；服务器上的多卡加速靠
    #    外层「一页一卡」并发，真上机时再补，本档位先只调大 batch_size。
    "musetalk-server": {
        "backend": "musetalk",
        "desc": "学校 8×A6000 48GB（Linux）—— 预留档位，未实机验证",
        "version": "v15",
        "fps": 25,
        "bbox_shift": 0,         # v15 下无效，见上
        "extra_margin": 10,
        "batch_size": 32,
        "parsing_mode": "jaw",
        "left_cheek_width": 90,
        "right_cheek_width": 90,
        "use_float16": True,
        "parallel": {"mode": "single"},
    },

    # ── LongCat（保留；本机 sm_120 跑不了，等 8×A6000）────────
    # 卡点：主注意力序列 36480，需要 flash-attn（无 sm_120 wheel）或 xformers
    # （kernel 只到 sm_90）。到了 sm_80 的 A6000 两者都有现成 wheel。
    #
    # 关键在 use_int8=True —— int8 DiT 15.1GB，是 16GB 卡上唯一能装下的形态；
    # 蒸馏 LoRA 把采样降到 8 步（v1.5 必带），否则 50 步在单卡上极慢。
    "local-16g": {
        "backend": "longcat",
        "desc": "本机 RTX 5070 Ti 16GB 单卡（Windows / sm_120）",
        "resolution": "480p",           # 832x480
        "use_int8": True,               # int8 DiT（15.1GB），16GB 卡命门
        "use_distill": True,            # DMD2 蒸馏，8 NFE（v1.5 必带）
        "num_inference_steps": 8,
        "text_guidance_scale": 1.0,     # 蒸馏模式下官方固定为 1.0
        "audio_guidance_scale": 1.0,
        "num_frames": 93,
        "parallel": {"mode": "single", "context_parallel_size": 1},
    },

    # 学校服务器：8×A6000 48GB
    # ⚠️ 预留档位，未实机验证。字段依据 LongCat 官方多卡用法：
    #    torchrun --nproc_per_node=8 ... --context_parallel_size=8
    # 单卡 48GB 放得下 bf16 DiT（30.2GB），可不量化、分辨率提到 720p。
    "server-8xa6000": {
        "backend": "longcat",
        "desc": "学校 8×A6000 48GB（Linux）—— 预留档位，未实机验证",
        "resolution": "720p",           # 1280x768
        "use_int8": False,              # 48GB 装得下 bf16
        "use_distill": True,
        "num_inference_steps": 8,
        "text_guidance_scale": 1.0,
        "audio_guidance_scale": 1.0,
        "num_frames": 93,
        "parallel": {"mode": "torchrun", "nproc_per_node": 8, "context_parallel_size": 8},
    },
}


# ──────────────────────────────────────────────────────────────
# 路径集合
# ──────────────────────────────────────────────────────────────

@dataclass
class TalkingPaths:
    """一套数字人运行所需的全部路径。

    刻意用 dataclass 而不是散落的字符串常量：迁移到服务器时，这里每个字段
    都可以从环境变量或配置读出来，调用方代码不变。

    字段在两个后端间的归属见各自注释 —— 只有 ``repo_dir`` 与 ``env_python``
    是两个后端都用的。
    """
    # —— 两个后端共用 ——
    repo_dir: str                 # 后端代码仓库根目录（也是运行 cwd）
    env_python: str               # 专用 venv 的解释器（对应上游 --talking_head_env）

    # —— 仅 longcat 使用 ——
    checkpoint_dir: str = ""      # Avatar 权重目录（内含 base_model_int8/lora/whisper）
    base_dir: str = ""            # 底模 LongCat-Video（tokenizer/UMT5/vae）
    infer_script: str = ""        # 精简推理入口，默认 repo_dir/avatar_infer.py

    # —— 仅 musetalk 使用 ——
    # ffmpeg 可执行文件所在目录。inference.py 只在 `ffmpeg -version` 失败时
    # 才把它前置进 PATH，所以给对目录即可；留空则沿用子进程现有 PATH。
    ffmpeg_dir: str = ""

    def resolved_base_dir(self) -> str:
        return self.base_dir or os.path.join(self.checkpoint_dir, "..", "LongCat-Video")

    def resolved_infer_script(self) -> str:
        return self.infer_script or os.path.join(self.repo_dir, "avatar_infer.py")

    def musetalk_models_dir(self) -> str:
        """MuseTalk 权重目录。

        ⚠️ 恒为 ``<repo_dir>/models``，**不可配置** —— vae.py 与
        face_parsing/__init__.py 把它写死成相对 cwd 的 ``models/...``，
        改到别处就加载不到。这个方法存在的意义是把该约束写在唯一一处。
        """
        return os.path.join(self.repo_dir, "models")


def resolve_profile(name: Optional[str] = None, backend: Optional[str] = None) -> dict:
    """按 显式入参 > 环境变量 > 后端默认 的顺序取档位配置。

    ``backend`` 只用于两件事：① 未指定档位时挑该后端的默认档；
    ② 校验拿到的档位确实属于该后端（防止把 longcat 的参数名传给 musetalk）。
    """
    if name:
        key = name
    else:
        key = os.environ.get("PAPER2VIDEO_TALKING_PROFILE") or ""
        if not key:
            key = DEFAULT_PROFILE_BY_BACKEND.get(backend or "", DEFAULT_PROFILE)
    if key not in PROFILES:
        raise KeyError(
            "未知的数字人档位 {!r}；可选：{}".format(key, ", ".join(sorted(PROFILES))))
    profile = dict(PROFILES[key])        # 浅拷贝，避免调用方改坏全局字典
    if backend and profile.get("backend") != backend:
        raise ValueError(
            "档位 {!r} 属于后端 {!r}，不能用于后端 {!r}".format(
                key, profile.get("backend"), backend))
    profile["name"] = key
    return profile


def build_avatar_command(profile: dict, paths: TalkingPaths,
                         config_path: str, output_dir: str,
                         extra_args: Optional[List[str]] = None) -> List[str]:
    """把档位 + 路径 + 本次任务翻译成完整的命令行（按 backend 分派）。

    多卡时返回 ``torchrun`` 打头的命令；单卡时直接返回解释器 + 脚本。
    **本函数不执行任何东西**，只负责拼装 —— 这样单卡/多卡的分支只存在于这一处。

    ``config_path`` 对 longcat 是 input.json，对 musetalk 是 inference YAML。
    """
    backend = profile.get("backend", "longcat")
    if backend == "musetalk":
        return build_musetalk_command(profile, paths, config_path, output_dir, extra_args)
    return build_longcat_command(profile, paths, config_path, output_dir, extra_args)


def _check_single_gpu(profile: dict, backend: str) -> None:
    parallel = profile.get("parallel") or {"mode": "single"}
    if parallel.get("mode") != "single":
        raise NotImplementedError(
            "{} 的多卡档位尚未实现：当前入口是单卡版本，不具备分布式初始化能力。"
            "请先用单卡档位，或在移植到服务器时补齐多卡入口。".format(backend))


def build_longcat_command(profile: dict, paths: TalkingPaths,
                          input_json: str, output_dir: str,
                          extra_args: Optional[List[str]] = None) -> List[str]:
    """LongCat-Video-Avatar-1.5 的命令行。"""
    _check_single_gpu(profile, "LongCat")

    cmd = [paths.env_python, paths.resolved_infer_script()]

    cmd += [
        "--input_json", input_json,
        "--checkpoint_dir", paths.checkpoint_dir,
        "--output_dir", output_dir,
        "--resolution", profile["resolution"],
        "--num_frames", str(profile.get("num_frames", 93)),
        "--num_inference_steps", str(profile.get("num_inference_steps", 8)),
        "--text_guidance_scale", str(profile.get("text_guidance_scale", 1.0)),
        "--audio_guidance_scale", str(profile.get("audio_guidance_scale", 1.0)),
    ]

    if profile.get("use_int8"):
        cmd += ["--use_int8"]
    if profile.get("use_distill"):
        cmd += ["--use_distill"]

    if extra_args:
        cmd += list(extra_args)
    return cmd


def build_musetalk_command(profile: dict, paths: TalkingPaths,
                           inference_yaml: str, result_dir: str,
                           extra_args: Optional[List[str]] = None) -> List[str]:
    """MuseTalk 的命令行。

    入口是 ``python -m scripts.inference``（**不是**脚本路径）—— 上游 README 的
    Windows 用法就是 `-m`，这样 cwd 会进 sys.path，`import musetalk` 才成立。

    输出位置由 ``output_vid_name`` 决定，落在 ``<result_dir>/<version>/`` 下；
    取回产物请用 :func:`musetalk_output_path`，不要在这里重复规则。
    """
    _check_single_gpu(profile, "MuseTalk")

    models_dir = paths.musetalk_models_dir()
    version = profile.get("version", "v15")

    # cwd 必须是 repo_dir（见模块 docstring 的隐藏约束）
    cmd = [
        paths.env_python, "-m", "scripts.inference",
        "--inference_config", inference_yaml,
        "--result_dir", result_dir,
        # 只显式给 unet（默认为 models/musetalkV15/...，但白纸黑字更稳）；
        # whisper 同理。vae_type 与 face-parse 是相对 cwd 的硬编码，不能传绝对路径。
        "--unet_model_path", os.path.join(models_dir, "musetalkV15", "unet.pth"),
        "--unet_config", os.path.join(models_dir, "musetalkV15", "musetalk.json"),
        "--whisper_dir", os.path.join(models_dir, "whisper"),
        "--version", version,
        "--fps", str(profile.get("fps", 25)),
        "--bbox_shift", str(profile.get("bbox_shift", 0)),
        "--extra_margin", str(profile.get("extra_margin", 10)),
        "--batch_size", str(profile.get("batch_size", 8)),
        "--parsing_mode", str(profile.get("parsing_mode", "jaw")),
        "--left_cheek_width", str(profile.get("left_cheek_width", 90)),
        "--right_cheek_width", str(profile.get("right_cheek_width", 90)),
        "--output_vid_name", MUSETALK_OUTPUT_NAME,
    ]

    if profile.get("use_float16"):
        cmd += ["--use_float16"]
    if paths.ffmpeg_dir:
        cmd += ["--ffmpeg_path", paths.ffmpeg_dir]

    if extra_args:
        cmd += list(extra_args)
    return cmd


# MuseTalk 推理的固定产物名（--output_vid_name）。取回路径的唯一真源在
# musetalk_output_path()，改这里就够。
MUSETALK_OUTPUT_NAME = "musetalk_out.mp4"


def musetalk_output_path(profile: dict, result_dir: str) -> str:
    """MuseTalk 会把产物写到 ``<result_dir>/<version>/<output_vid_name>``。

    依据 scripts/inference.py:104-116：``temp_dir = join(result_dir, version)``，
    再与 ``output_vid_name`` 拼接。
    """
    return os.path.join(result_dir, profile.get("version", "v15"), MUSETALK_OUTPUT_NAME)


def describe_profile(profile: dict) -> str:
    """一行摘要，用于日志里说清「这次按什么档位跑」。"""
    backend = profile.get("backend", "longcat")
    parallel = profile.get("parallel") or {}
    mode = parallel.get("mode", "single")
    if mode == "torchrun":
        par = "torchrun×{} CP={}".format(
            parallel.get("nproc_per_node", 1), parallel.get("context_parallel_size", 1))
    else:
        par = "单卡串行"

    if backend == "musetalk":
        detail = "{} | {}fps | bbox_shift={} | {}".format(
            profile.get("version", "v15"),
            profile.get("fps", 25),
            profile.get("bbox_shift", 0),
            "fp16" if profile.get("use_float16") else "fp32")
    else:
        quant = "int8" if profile.get("use_int8") else "bf16"
        distill = "DMD2蒸馏" if profile.get("use_distill") else "全步采样"
        detail = "{} | {} | {} | {}步".format(
            profile["resolution"], quant, distill,
            profile.get("num_inference_steps", 8))

    return "[{}] {} | {} | {}".format(
        profile.get("name"), backend, detail, par)


def list_profiles() -> str:
    lines = ["可用数字人档位（按后端分组）："]
    for backend in ("musetalk", "longcat"):
        lines.append("")
        lines.append("  ── backend={} ──".format(backend))
        for key, value in PROFILES.items():
            if value.get("backend") != backend:
                continue
            mark = "（默认）" if key == DEFAULT_PROFILE_BY_BACKEND.get(backend) else ""
            lines.append("    - {}{}：{}".format(key, mark, value.get("desc", "")))
            lines.append("        {}".format(describe_profile(dict(value, name=key))))
    return "\n".join(lines)

