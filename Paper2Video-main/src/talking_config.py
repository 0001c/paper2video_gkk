"""数字人（talking head）运行档位 —— 面向「换机器只改配置」的移植接口层。

为什么需要这一层
----------------
本机是 **RTX 5070 Ti 16GB（sm_120 / Windows）**，后续要迁到学校的
**8×A6000 48GB 服务器**。两边的差别是多维的：分辨率、量化方式、显存策略、
并行方式、权重根目录。如果把这些差异写成 `if 在服务器上:` 的代码分支，
迁移时就要改代码、就要重新测；收敛成 profile 之后，**迁移只改一个字符串**。

本层的职责边界（务必看清）
--------------------------
**只做「profile → 命令行参数」的翻译，不实现、也不验证真实的多卡并行。**
`server-8xa6000` 档位的字段是按 InfiniteTalk 官方多卡用法（torchrun + FSDP +
Ulysses 序列并行）填写的，属于**预留接口**。真上机时若发现字段有出入，
只改本文件 `PROFILES` 字典即可，`talking_gen.py` 与 `pipeline.py` 都不用动。

切换档位的三种方式（优先级从高到低）
------------------------------------
1. `talking_gen_per_slide(..., profile="server-8xa6000")` 显式传参
2. 环境变量 `PAPER2VIDEO_TALKING_PROFILE=server-8xa6000`（写在 `.env` 里）
3. 上面的 `DEFAULT_PROFILE`（本机默认 local-16g）

本机实测要点（2026-09-17）
--------------------------
- `numpy<2` 是硬约束 → InfiniteTalk 必须用 **Python 3.12**（3.13 没有 numpy 1.x 的 wheel）
- torch 必须从 pytorch 官方 **cu128** 源装：PyPI 上 Windows 的 torch 是 CPU-only，
  且 Blackwell(sm_120) 只有 cu128 及以后才有编译目标
- 仓库内自带 `kokoro/`、`wan/`、`src/`，因此**必须以仓库根目录为 cwd** 运行
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
    # 关键在 num_persistent_param_in_dit=0 —— fp8 的 DiT 权重仍有 18.2GB，
    # 超过 16GB 显存，必须让参数「随用随载」而不是常驻。
    "local-16g": {
        "desc": "本机 RTX 5070 Ti 16GB 单卡（Windows / sm_120）",
        "size": "infinitetalk-480",          # 640x640
        "quant": "fp8",                       # 用 quant_models/*_fp8.safetensors
        "sample_steps": 40,
        "mode": "streaming",                  # 每页音频 14~20s > 单块 81 帧(约5s)，必须流式
        "motion_frame": 9,
        "num_persistent_param_in_dit": 0,     # 16GB 卡的命门
        "offload_model": True,
        "t5_cpu": False,
        "sample_audio_guide_scale": 4.0,      # 官方建议 3~5
        "sample_text_guide_scale": 5.0,
        "use_teacache": False,                # 先用稳定档，跑通后再考虑开加速
        "parallel": {"mode": "single", "nproc_per_node": 1},
    },

    # 学校服务器：8×A6000 48GB
    # ⚠️ 预留档位，未实机验证。字段依据 InfiniteTalk 官方多卡用法：
    #    torchrun --nproc_per_node=8 --standalone generate_infinitetalk.py \
    #        ... --dit_fsdp --t5_fsdp --ulysses_size=8
    # 单卡 48GB 足以放下 bf16 的 DiT 分片，因此可不量化、并把分辨率提到 960x960。
    "server-8xa6000": {
        "desc": "学校 8×A6000 48GB（Linux）—— 预留档位，未实机验证",
        "size": "infinitetalk-720",           # 960x960
        "quant": None,                        # 48GB 装得下 bf16，无需量化
        "sample_steps": 40,
        "mode": "streaming",
        "motion_frame": 9,
        "num_persistent_param_in_dit": None,  # 不限制常驻参数
        "offload_model": False,               # 显存充足，别用 offload 拖速度
        "t5_cpu": False,
        "sample_audio_guide_scale": 4.0,
        "sample_text_guide_scale": 5.0,
        "use_teacache": False,
        "parallel": {
            "mode": "torchrun",
            "nproc_per_node": 8,
            "dit_fsdp": True,
            "t5_fsdp": True,
            "ulysses_size": 8,                # 序列并行度，官方示例取卡数
            "ring_size": 1,
        },
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
    repo_dir: str                 # InfiniteTalk 代码仓库根目录（也是运行 cwd）
    env_python: str               # 专用 venv 的解释器（对应上游 --talking_head_env）
    ckpt_dir: str                 # Wan2.1 底模：config.json / VAE / CLIP / tokenizer
    wav2vec_dir: str              # chinese-wav2vec2-base 音频编码器
    quant_dir: str = ""           # 量化权重所在目录
    infinitetalk_dir: str = ""    # 非量化路径才用（量化分支不加载它）
    dit_path: str = ""            # 非量化时的单体 DiT 权重
    lora_dir: str = ""
    generate_script: str = ""     # 默认 repo_dir/generate_infinitetalk.py

    def resolved_generate_script(self) -> str:
        return self.generate_script or os.path.join(self.repo_dir, "generate_infinitetalk.py")


def resolve_profile(name: Optional[str] = None) -> dict:
    """按 显式入参 > 环境变量 > 默认 的顺序取档位配置。"""
    key = name or os.environ.get("PAPER2VIDEO_TALKING_PROFILE") or DEFAULT_PROFILE
    if key not in PROFILES:
        raise KeyError(
            "未知的数字人档位 {!r}；可选：{}".format(key, ", ".join(sorted(PROFILES))))
    profile = dict(PROFILES[key])        # 浅拷贝，避免调用方改坏全局字典
    profile["name"] = key
    return profile


def build_infinitetalk_command(profile: dict, paths: TalkingPaths,
                               input_json: str, save_file: str,
                               extra_args: Optional[List[str]] = None) -> List[str]:
    """把档位 + 路径 + 本次任务翻译成完整的命令行。

    多卡时返回 ``torchrun`` 打头的命令；单卡时直接返回解释器 + 脚本。
    **本函数不执行任何东西**，只负责拼装 —— 这样单卡/多卡的分支只存在于这一处。
    """
    script = paths.resolved_generate_script()
    parallel = profile.get("parallel") or {"mode": "single"}

    if parallel.get("mode") == "torchrun":
        cmd = [
            "torchrun",
            "--nproc_per_node={}".format(parallel.get("nproc_per_node", 1)),
            "--standalone",
            script,
        ]
    else:
        cmd = [paths.env_python, script]

    cmd += [
        "--task", "infinitetalk-14B",
        "--size", profile["size"],
        "--ckpt_dir", paths.ckpt_dir,
        "--wav2vec_dir", paths.wav2vec_dir,
        "--input_json", input_json,
        "--save_file", save_file,
        "--mode", profile.get("mode", "streaming"),
        "--motion_frame", str(profile.get("motion_frame", 9)),
        "--sample_steps", str(profile.get("sample_steps", 40)),
        "--sample_audio_guide_scale", str(profile.get("sample_audio_guide_scale", 4.0)),
        "--sample_text_guide_scale", str(profile.get("sample_text_guide_scale", 5.0)),
    ]

    # 量化：--quant 与 --quant_dir 必须成对出现
    if profile.get("quant"):
        cmd += ["--quant", profile["quant"],
                "--quant_dir", os.path.join(
                    paths.quant_dir or paths.infinitetalk_dir,
                    "infinitetalk_single_{}.safetensors".format(profile["quant"]))]

    # 显存策略。注意 --offload_model 是 type=str2bool，**必须带值**，不是开关
    persistent = profile.get("num_persistent_param_in_dit")
    if persistent is not None:
        cmd += ["--num_persistent_param_in_dit", str(persistent)]
    if profile.get("offload_model") is not None:
        cmd += ["--offload_model", "true" if profile["offload_model"] else "false"]
    if profile.get("t5_cpu"):
        cmd += ["--t5_cpu"]
    if profile.get("use_teacache"):
        cmd += ["--use_teacache"]

    # 非量化路径才需要这两个
    if not profile.get("quant"):
        if paths.infinitetalk_dir:
            cmd += ["--infinitetalk_dir",
                    os.path.join(paths.infinitetalk_dir, "infinitetalk.safetensors")]
        if paths.dit_path:
            cmd += ["--dit_path", paths.dit_path]

    # 多卡并行（预留）
    if parallel.get("dit_fsdp"):
        cmd += ["--dit_fsdp"]
    if parallel.get("t5_fsdp"):
        cmd += ["--t5_fsdp"]
    if parallel.get("ulysses_size"):
        cmd += ["--ulysses_size", str(parallel["ulysses_size"])]
    if parallel.get("ring_size"):
        cmd += ["--ring_size", str(parallel["ring_size"])]

    if lora := paths.lora_dir:
        cmd += ["--lora_dir", lora]

    if extra_args:
        cmd += list(extra_args)
    return cmd


def describe_profile(profile: dict) -> str:
    """一行摘要，用于日志里说清「这次按什么档位跑」。"""
    parallel = profile.get("parallel") or {}
    mode = parallel.get("mode", "single")
    if mode == "torchrun":
        par = "torchrun×{} FSDP+Ulysses{}".format(
            parallel.get("nproc_per_node", 1), parallel.get("ulysses_size", 1))
    else:
        par = "单卡串行"
    quant = profile.get("quant") or "不量化"
    persistent = profile.get("num_persistent_param_in_dit")
    vram = "常驻参数不限" if persistent is None else "常驻参数={}".format(persistent)
    return "[{}] {} | {} | {} | {} | {}".format(
        profile.get("name"), profile["size"], quant, par, vram, profile.get("desc", ""))


def list_profiles() -> str:
    lines = ["可用数字人档位："]
    for key, value in PROFILES.items():
        mark = "（默认）" if key == DEFAULT_PROFILE else ""
        lines.append("  - {}{}：{}".format(key, mark, value.get("desc", "")))
        lines.append("      {}".format(describe_profile(dict(value, name=key))))
    return "\n".join(lines)
