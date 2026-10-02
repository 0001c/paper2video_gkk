"""MuseTalk 单卡运行档位与命令构建。

权重必须位于上游仓库 models/ 下；保留单页及批量入口的路径契约。
"""

import os
from dataclasses import dataclass
from typing import List, Optional

DEFAULT_PROFILE = "musetalk-local"
MUSETALK_OUTPUT_NAME = "musetalk_out.mp4"
PROFILES = {'musetalk-local': {'backend': 'musetalk',
                    'desc': '本机 RTX 5070 Ti 16GB 单卡（Windows / sm_120）',
                    'version': 'v15',
                    'fps': 25,
                    'bbox_shift': 0,
                    'extra_margin': 10,
                    'batch_size': 8,
                    'parsing_mode': 'jaw',
                    'left_cheek_width': 90,
                    'right_cheek_width': 90,
                    'use_float16': True,
                    'batch': True,
                    'parallel': {'mode': 'single'}}}


@dataclass
class TalkingPaths:
    """MuseTalk 仓库、解释器与 FFmpeg 路径。"""
    repo_dir: str
    env_python: str
    ffmpeg_dir: str = ""

    def musetalk_models_dir(self) -> str:
        return os.path.join(self.repo_dir, "models")


def resolve_profile(name: Optional[str] = None, backend: Optional[str] = None) -> dict:
    """按显式档位、环境配置、默认档位的顺序解析。"""
    if backend not in (None, "musetalk"):
        raise ValueError("不支持的数字人后端 {!r}（当前仅支持 musetalk）".format(backend))
    key = name or os.environ.get("PAPER2VIDEO_TALKING_PROFILE") or DEFAULT_PROFILE
    if key not in PROFILES:
        raise KeyError("未知的数字人档位 {!r}；可选：{}".format(key, ", ".join(sorted(PROFILES))))
    return dict(PROFILES[key], name=key)


def build_avatar_command(profile: dict, paths: TalkingPaths,
                         config_path: str, output_dir: str,
                         extra_args: Optional[List[str]] = None) -> List[str]:
    """构建当前数字人后端的单页命令，不执行推理。"""
    if profile.get("backend", "musetalk") != "musetalk":
        raise ValueError("当前仅支持 musetalk 数字人后端")
    return build_musetalk_command(profile, paths, config_path, output_dir, extra_args)


def _check_single_gpu(profile: dict, backend: str) -> None:
    parallel = profile.get("parallel") or {"mode": "single"}
    if parallel.get("mode") != "single":
        raise NotImplementedError(
            "{} 的多卡档位尚未实现：当前入口是单卡版本，不具备分布式初始化能力。"
            "请先用单卡档位，或在移植到服务器时补齐多卡入口。".format(backend))


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


def build_musetalk_batch_command(profile: dict, paths: TalkingPaths,
                                 inference_yaml: str,
                                 extra_args: Optional[List[str]] = None) -> List[str]:
    """MuseTalk **批量**入口（``scripts.realtime_inference``）的命令行。

    为什么用它
    ----------
    单页入口（``scripts.inference``）每页新起一个进程，于是 import torch +
    加载 3.2GB unet + 加载 whisper + 人脸检测/解析/VAE 编码 latent **每页都重做一遍**。
    实测（12 页真实数据）每页耗时对音频时长回归得

        耗时 ≈ 82.4 s + 3.05 × 音频秒

    即**每页有约 82 秒与音频长度无关的固定开销**，占 12 页总时间的约 59%。
    批量入口把这些一次性做完：模型加载一次，每个 avatar 的 ``latents.pt``
    缓存一次后复用给它的所有音频。

    与单页入口的差异（都是上游实现决定的，不是我们改的）
    ----------------------------------------------------
    - ``--unet_config`` 的**默认值是 v1 的路径**（``models/musetalk/musetalk.json``），
      必须显式覆盖成 v15 的，否则加载失败。
    - **没有 ``--use_float16``**：它硬编码 ``pe/vae/unet .half()``，所以批量路径
      恒为 fp16。
    - ``bbox_shift`` 在 v15 下同样被强制为 0。
    - **产物目录不认 ``--result_dir``**：``Avatar.base_path`` 硬编码为
      ``./results/<version>/avatars/<avatar_id>``（相对 cwd）。所以产物要按
      :func:`musetalk_batch_output_path` 去 MuseTalk 仓库里取。
    """
    _check_single_gpu(profile, "MuseTalk")

    models_dir = paths.musetalk_models_dir()
    cmd = [
        paths.env_python, "-m", "scripts.realtime_inference",
        "--inference_config", inference_yaml,
        # 上游默认值是 v1 的路径，必须显式覆盖（见 docstring）
        "--unet_model_path", os.path.join(models_dir, "musetalkV15", "unet.pth"),
        "--unet_config", os.path.join(models_dir, "musetalkV15", "musetalk.json"),
        "--whisper_dir", os.path.join(models_dir, "whisper"),
        "--version", profile.get("version", "v15"),
        "--fps", str(profile.get("fps", 25)),
        "--batch_size", str(profile.get("batch_size", 8)),
        "--extra_margin", str(profile.get("extra_margin", 10)),
        "--parsing_mode", str(profile.get("parsing_mode", "jaw")),
        "--left_cheek_width", str(profile.get("left_cheek_width", 90)),
        "--right_cheek_width", str(profile.get("right_cheek_width", 90)),
    ]
    if paths.ffmpeg_dir:
        cmd += ["--ffmpeg_path", paths.ffmpeg_dir]
    if extra_args:
        cmd += list(extra_args)
    return cmd


def musetalk_batch_dir(repo_dir: str, profile: dict, avatar_id: str) -> str:
    """批量入口里某个 avatar 的 scratch 目录（上游硬编码在 ``./results`` 下）。"""
    return os.path.join(repo_dir, "results", profile.get("version", "v15"),
                        "avatars", avatar_id)


def musetalk_batch_output(repo_dir: str, profile: dict, avatar_id: str, key: str) -> str:
    """批量入口的产物路径：``<avatar_dir>/vid_output/<key>.mp4``。

    ``key`` 就是 YAML 里 ``audio_clips`` 的键（我们用它当页码标识）。
    依据 realtime_inference.py:300 的 ``join(self.video_out_path, out_vid_name + ".mp4")``。
    """
    return os.path.join(musetalk_batch_dir(repo_dir, profile, avatar_id),
                        "vid_output", key + ".mp4")


def musetalk_output_path(profile: dict, result_dir: str) -> str:
    """MuseTalk 会把产物写到 ``<result_dir>/<version>/<output_vid_name>``。

    依据 scripts/inference.py:104-116：``temp_dir = join(result_dir, version)``，
    再与 ``output_vid_name`` 拼接。
    """
    return os.path.join(result_dir, profile.get("version", "v15"), MUSETALK_OUTPUT_NAME)


def describe_profile(profile: dict) -> str:
    """返回当前单卡档位的日志摘要。"""
    detail = "{} | {}fps | bbox_shift={} | {}".format(
        profile.get("version", "v15"), profile.get("fps", 25),
        profile.get("bbox_shift", 0), "fp16" if profile.get("use_float16") else "fp32")
    return "[{}] musetalk | {} | 单卡串行".format(profile.get("name"), detail)


def list_profiles() -> str:
    lines = ["可用数字人档位："]
    for name, profile in PROFILES.items():
        mark = "（默认）" if name == DEFAULT_PROFILE else ""
        lines.append("  - {}{}：{}".format(name, mark, profile.get("desc", "")))
        lines.append("      {}".format(describe_profile(dict(profile, name=name))))
    return "\n".join(lines)
