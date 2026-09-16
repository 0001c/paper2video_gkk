"""数字人（talking head）运行档位 —— 面向「换机器只改配置」的移植接口层。

后端：LongCat-Video-Avatar-1.5（美团，MIT）
------------------------------------------------
2026-09-17 换型，替代 InfiniteTalk（其 fp8 权重加载会触发 quanto 的 marlin CUDA
扩展 JIT 编译，而本机 nvcc 12.6 无 compute_120 目标，详见 docs/模型替换方案.md）。

为什么需要这一层
----------------
本机是 **RTX 5070 Ti 16GB（sm_120 / Windows）**，后续要迁到学校的
**8×A6000 48GB 服务器**。两边的差别是多维的：分辨率、量化方式、显存策略、
并行方式、权重根目录。收敛成 profile 之后，**迁移只改一个字符串**。

本层的职责边界（务必看清）
--------------------------
**只做「profile → 命令行参数」的翻译，不实现、也不验证真实的多卡并行。**
`server-8xa6000` 档位的字段是按 LongCat 官方多卡用法（torchrun + context_parallel）
填写的，属于**预留接口**。真上机时若发现字段有出入，只改本文件 `PROFILES`
字典即可，`talking_gen.py` 与 `pipeline.py` 都不用动。

切换档位的三种方式（优先级从高到低）
------------------------------------
1. `talking_gen_per_slide(..., profile="server-8xa6000")` 显式传参
2. 环境变量 `PAPER2VIDEO_TALKING_PROFILE=server-8xa6000`（写在 `.env` 里）
3. 上面的 `DEFAULT_PROFILE`（本机默认 local-16g）
"""

import os
from dataclasses import dataclass, field
from typing import List, Optional

DEFAULT_PROFILE = "local-16g"

# ──────────────────────────────────────────────────────────────
# 档位定义
# ──────────────────────────────────────────────────────────────
PROFILES = {
    # 本机：RTX 5070 Ti 16GB，单卡串行
    # 关键在 use_int8=True —— int8 DiT 15.1GB，是 16GB 卡上唯一能装下的形态；
    # 蒸馏 LoRA 把采样降到 8 步（v1.5 必带），否则 50 步在单卡上极慢。
    "local-16g": {
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
    """
    repo_dir: str                 # LongCat-Video 代码仓库根目录（也是运行 cwd）
    env_python: str               # 专用 venv 的解释器（对应上游 --talking_head_env）
    checkpoint_dir: str           # Avatar 权重目录（内含 base_model_int8/lora/whisper）
    base_dir: str = ""            # 底模 LongCat-Video（tokenizer/UMT5/vae），默认 checkpoint_dir/../LongCat-Video
    infer_script: str = ""        # 精简推理入口，默认 repo_dir/avatar_infer.py

    def resolved_base_dir(self) -> str:
        return self.base_dir or os.path.join(self.checkpoint_dir, "..", "LongCat-Video")

    def resolved_infer_script(self) -> str:
        return self.infer_script or os.path.join(self.repo_dir, "avatar_infer.py")


def resolve_profile(name: Optional[str] = None) -> dict:
    """按 显式入参 > 环境变量 > 默认 的顺序取档位配置。"""
    key = name or os.environ.get("PAPER2VIDEO_TALKING_PROFILE") or DEFAULT_PROFILE
    if key not in PROFILES:
        raise KeyError(
            "未知的数字人档位 {!r}；可选：{}".format(key, ", ".join(sorted(PROFILES))))
    profile = dict(PROFILES[key])        # 浅拷贝，避免调用方改坏全局字典
    profile["name"] = key
    return profile


def build_avatar_command(profile: dict, paths: TalkingPaths,
                         input_json: str, output_dir: str,
                         extra_args: Optional[List[str]] = None) -> List[str]:
    """把档位 + 路径 + 本次任务翻译成完整的命令行。

    多卡时返回 ``torchrun`` 打头的命令；单卡时直接返回解释器 + 脚本。
    **本函数不执行任何东西**，只负责拼装 —— 这样单卡/多卡的分支只存在于这一处。
    """
    script = paths.resolved_infer_script()
    parallel = profile.get("parallel") or {"mode": "single"}

    if parallel.get("mode") == "torchrun":
        # 多卡是预留接口：avatar_infer.py 目前只实现单卡（无 --context_parallel_size、
        # 无分布式初始化）。真上服务器时需按 LongCat 官方 demo 补齐这些能力，
        # 在那之前明确报错，避免拼出无效命令被静默当成「单卡」跑。
        raise NotImplementedError(
            "server 多卡档位尚未实现：avatar_infer.py 当前是单卡版本，"
            "不具备 --context_parallel_size / 分布式初始化能力。"
            "请先用 local-16g，或在移植到 8×A6000 时补齐多卡入口。")

    cmd = [paths.env_python, script]

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


def describe_profile(profile: dict) -> str:
    """一行摘要，用于日志里说清「这次按什么档位跑」。"""
    parallel = profile.get("parallel") or {}
    mode = parallel.get("mode", "single")
    if mode == "torchrun":
        par = "torchrun×{} CP={}".format(
            parallel.get("nproc_per_node", 1), parallel.get("context_parallel_size", 1))
    else:
        par = "单卡串行"
    quant = "int8" if profile.get("use_int8") else "bf16"
    distill = "DMD2蒸馏" if profile.get("use_distill") else "全步采样"
    return "[{}] {} | {} | {} | {}步 | {}".format(
        profile.get("name"), profile["resolution"], quant, distill,
        profile.get("num_inference_steps", 8), profile.get("desc", ""))


def list_profiles() -> str:
    lines = ["可用数字人档位："]
    for key, value in PROFILES.items():
        mark = "（默认）" if key == DEFAULT_PROFILE else ""
        lines.append("  - {}{}：{}".format(key, mark, value.get("desc", "")))
        lines.append("      {}".format(describe_profile(dict(value, name=key))))
    return "\n".join(lines)
