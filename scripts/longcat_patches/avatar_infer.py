#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""LongCat-Video-Avatar-1.5 单卡精简推理入口（Windows 原生适配）。

为什么不用上游的 run_demo_avatar_single_audio_to_video.py
----------------------------------------------------------
那个 demo 有四处**只能在 Linux / 多卡 torchrun 环境**下运行的硬伤：

1. ``os.environ['RANK']``（第 98 行）—— 单卡单进程没有这个变量，直接 KeyError；
2. 无条件 ``dist.init_process_group(backend="nccl")``（第 102 行）—— 单卡也要
   初始化分布式，Windows 上 nccl backend 也不稳；
3. ``os.system(f"mv '{a}' '{b}'")``（第 50 行）—— bash 语法，Windows 跑不了；
4. 输出到 ``/tmp/temp_speech_...``（第 182 行）—— Linux 路径。

本脚本走**同一套 pipeline API**（LongCatVideoAvatarPipeline），但把上面四处
拆掉：不碰分布式、不用 bash、输出到参数指定的目录。语义与 demo 完全一致。

用法（在 LongCat 专用 venv 里跑）：

    python avatar_infer.py --input_json <json> --checkpoint_dir <dir> --output_dir <dir>

输入 JSON 与上游约定一致：

    {"prompt": "...", "cond_image": "<参考图>",
     "cond_audio": {"person1": "<音频>"}}
"""

import argparse
import json
import math
import os
import sys
import time

import numpy as np
import PIL.Image

# LongCat 必须以仓库根为 cwd 才能 import（仓库内自带 longcat_video/ 包）
LONGCAT_ROOT = os.path.dirname(os.path.abspath(__file__))
if LONGCAT_ROOT not in sys.path:
    sys.path.insert(0, LONGCAT_ROOT)

import torch  # noqa: E402
import librosa  # noqa: E402
from transformers import AutoTokenizer, UMT5EncoderModel  # noqa: E402
from diffusers.utils import load_image  # noqa: E402

from longcat_video.pipeline_longcat_video_avatar import LongCatVideoAvatarPipeline  # noqa: E402
from longcat_video.modules.scheduling_flow_match_euler_discrete import FlowMatchEulerDiscreteScheduler  # noqa: E402
from longcat_video.modules.autoencoder_kl_wan import AutoencoderKLWan  # noqa: E402
from longcat_video.modules.avatar.longcat_video_dit_avatar import LongCatVideoAvatarTransformer3DModel  # noqa: E402
from longcat_video.modules.quantization import load_quantized_dit  # noqa: E402
from longcat_video.audio_process import get_audio_encoder, get_audio_feature_extractor  # noqa: E402
from longcat_video.audio_process.torch_utils import save_video_ffmpeg  # noqa: E402


NEGATIVE_PROMPT = (
    "Close-up, Bright tones, overexposed, static, blurred details, subtitles, style, "
    "works, paintings, images, static, overall gray, worst quality, low quality, "
    "JPEG compression residue, ugly, incomplete, extra fingers, poorly drawn hands, "
    "poorly drawn faces, deformed, disfigured, misshapen limbs, fused fingers, still "
    "picture, messy background, three legs, many people in the background, walking "
    "backwards"
)


def build_pipeline(args, device):
    checkpoint_dir = args.checkpoint_dir
    base_dir = os.path.join(checkpoint_dir, "..", "LongCat-Video")

    tokenizer = AutoTokenizer.from_pretrained(
        base_dir, subfolder="tokenizer", torch_dtype=torch.bfloat16)
    text_encoder = UMT5EncoderModel.from_pretrained(
        base_dir, subfolder="text_encoder", torch_dtype=torch.bfloat16)
    vae = AutoencoderKLWan.from_pretrained(
        base_dir, subfolder="vae", torch_dtype=torch.bfloat16)
    scheduler = FlowMatchEulerDiscreteScheduler.from_pretrained(
        checkpoint_dir, subfolder="scheduler", torch_dtype=torch.bfloat16)

    # 只支持 v1.5（v1.0 的 wav2vec2 与无限时长需求不匹配，见方案文档）
    model_type = "avatar-v1.5"
    # 单卡：cp_split_hw 必须显式给 [1, 1] 而非 None —— 模型的 forward() 会无条件
    # 下标访问 self.cp_split_hw[0]（longcat_video_dit_avatar.py:475）。上游 demo 里
    # 这个值来自 get_optimal_split(cp_size)，单卡时 cp_size=1 -> [1,1]；我们省掉了
    # 分布式初始化，就必须手动补上这个值，否则 forward 直接 TypeError。
    cp_split_hw = [1, 1]
    if args.use_int8:
        print("[avatar_infer] 加载 INT8 量化 DiT", flush=True)
        dit = load_quantized_dit(checkpoint_dir, subfolder="base_model_int8",
                                 cp_split_hw=cp_split_hw)
    else:
        dit = LongCatVideoAvatarTransformer3DModel.from_pretrained(
            checkpoint_dir, subfolder="base_model", cp_split_hw=cp_split_hw,
            torch_dtype=torch.bfloat16)

    if args.use_distill:
        lora_path = os.path.join(checkpoint_dir, "lora", "dmd_lora.safetensors")
        if os.path.exists(lora_path):
            dit.load_lora(lora_path, "dmd", multiplier=1.0,
                          lora_network_dim=128, lora_network_alpha=64)
            dit.enable_loras(["dmd"])
            print("[avatar_infer] 已启用 DMD2 蒸馏 LoRA（8 步采样）", flush=True)
        else:
            print("[avatar_infer] 未找到 dmd_lora，跳过蒸馏", flush=True)

    audio_path = os.path.join(checkpoint_dir, "whisper-large-v3")
    audio_encoder = get_audio_encoder(audio_path, model_type).to(device)
    audio_feature_extractor = get_audio_feature_extractor(audio_path, model_type)

    pipe = LongCatVideoAvatarPipeline(
        tokenizer=tokenizer, text_encoder=text_encoder, vae=vae,
        scheduler=scheduler, dit=dit, audio_encoder=audio_encoder,
        audio_feature_extractor=audio_feature_extractor, model_type=model_type)
    pipe.to(device)
    return pipe, model_type


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input_json", required=True)
    parser.add_argument("--checkpoint_dir", required=True)
    parser.add_argument("--output_dir", required=True)
    parser.add_argument("--resolution", default="480p", choices=["480p", "720p"])
    parser.add_argument("--num_frames", type=int, default=93)
    parser.add_argument("--num_inference_steps", type=int, default=8)
    parser.add_argument("--text_guidance_scale", type=float, default=1.0)
    parser.add_argument("--audio_guidance_scale", type=float, default=1.0)
    parser.add_argument("--use_distill", action="store_true")
    parser.add_argument("--use_int8", action="store_true")
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")
    if device.type != "cuda":
        print("[avatar_infer] 未检测到 CUDA，可能无法运行（需要 GPU）", flush=True)

    with open(args.input_json, encoding="utf-8") as handle:
        data = json.load(handle)
    prompt = data["prompt"]
    image_path = data["cond_image"]
    audio_path = data["cond_audio"]["person1"]

    os.makedirs(args.output_dir, exist_ok=True)

    t0 = time.time()
    pipe, model_type = build_pipeline(args, device)
    print("[avatar_infer] 模型加载完成，用时 {:.0f}s".format(time.time() - t0), flush=True)

    # 音频时长决定视频时长；v1.5 用 25fps
    save_fps = 25
    audio_stride = 1

    speech_array, sr = librosa.load(audio_path, sr=16000)
    source_duration = len(speech_array) / sr
    video_length = int(source_duration * save_fps)

    # 生成帧数按音频时长向上取整到 num_frames 的整数倍（不足的补静音）
    num_segments = max(1, math.ceil(video_length / args.num_frames))
    generate_duration = args.num_frames / save_fps * num_segments
    added = math.ceil((generate_duration - source_duration) * sr)
    if added > 0:
        speech_array = np.append(speech_array, [0.0] * added)
    print("[avatar_infer] 音频 {:.2f}s -> {} 段 × {} 帧（fps={}）".format(
        source_duration, num_segments, args.num_frames, save_fps), flush=True)

    full_audio_emb = pipe.get_audio_embedding(
        speech_array, fps=save_fps * audio_stride, device=device,
        sample_rate=sr, model_type=model_type)

    # 取第一段的音频嵌入（演示需求：单段已覆盖 14~20s 的页音频，这里保守按单段）
    indices = torch.arange(2 * 2 + 1) - 2
    audio_end = audio_stride * args.num_frames
    center = torch.arange(0, audio_end, audio_stride).unsqueeze(1) + indices.unsqueeze(0)
    center = torch.clamp(center, min=0, max=full_audio_emb.shape[0] - 1)
    audio_emb = full_audio_emb[center][None, ...].to(device)

    generator = torch.Generator(device=device).manual_seed(args.seed)
    image = load_image(image_path)

    t1 = time.time()
    output, latent = pipe.generate_ai2v(
        image=image, prompt=prompt, negative_prompt=NEGATIVE_PROMPT,
        resolution=args.resolution, num_frames=args.num_frames,
        num_inference_steps=args.num_inference_steps,
        text_guidance_scale=args.text_guidance_scale,
        audio_guidance_scale=args.audio_guidance_scale,
        output_type="both", generator=generator, audio_emb=audio_emb,
        use_distill=args.use_distill,
    )
    print("[avatar_infer] 生成完成，用时 {:.0f}s".format(time.time() - t1), flush=True)

    frames = output[0]
    video = [(frames[i] * 255).astype(np.uint8) for i in range(frames.shape[0])]
    video = [PIL.Image.fromarray(img) for img in video]
    out_tensor = torch.from_numpy(np.array(video))

    # save_path 不给扩展名，save_video_ffmpeg 内部会补 .mp4 并混入音轨
    save_prefix = os.path.join(args.output_dir, "avatar_out")
    save_video_ffmpeg(out_tensor, save_prefix, audio_path, fps=save_fps, quality=5)
    final = save_prefix + ".mp4"
    print("[avatar_infer] 已保存 -> {}".format(final), flush=True)

    # 供上层核对产物的明确信号
    print("AVATAR_OUTPUT={}".format(final), flush=True)


if __name__ == "__main__":
    main()
