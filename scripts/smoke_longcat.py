#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""LongCat 数字人单页冒烟测试。

目标：验证最大未知数 —— int8 DiT（15.1GB）+ Whisper + UMT5 + VAE 能否在
RTX 5070 Ti 16GB 上装下并跑出一段带音轨的视频。

用法（在 LongCat 专用 venv 里跑）：

    .venv/Scripts/python.exe smoke_longcat.py

它复用 talking_gen.py 的暂存/输入构造逻辑，但**直接调用** avatar_infer.py，
不经过 subprocess（这样能拿到进程内真实的显存峰值，而不是看子进程的）。
"""

import json
import os
import sys
import time

# 让脚本能从项目 src 导入 talking_gen（仅借用暂存逻辑，不触发 subprocess）
PROJECT_SRC = os.environ.get(
    "PAPER2VIDEO_SRC", r"E:\Development\26年\paper2video\Paper2Video-main\src")
sys.path.insert(0, PROJECT_SRC)

import pv_env  # noqa: E402
import talking_gen as tg  # noqa: E402

LONGCAT_ROOT = os.environ.get("PAPER2VIDEO_LONGCAT_ROOT", r"E:\ai-models\LongCat-Video")
sys.path.insert(0, LONGCAT_ROOT)

REF_IMG = os.environ.get(
    "PAPER2VIDEO_SMOKE_REF",
    os.path.join(PROJECT_SRC, "..", "assets", "demo", "zeyu.png"))
REF_AUDIO = os.environ.get(
    "PAPER2VIDEO_SMOKE_AUDIO",
    os.path.join(PROJECT_SRC, "..", "assets", "demo", "zeyu.wav"))


def vram_mb():
    import torch
    return torch.cuda.memory_allocated() / 2 ** 20


def main():
    import torch
    from avatar_infer import build_pipeline  # noqa: E402

    paths = tg.resolve_paths()
    checkpoint_dir = paths.checkpoint_dir

    # 1) 暂存输入（复用 talking_gen 的逻辑，含透明通道白底合成）
    workspace = tg._ascii_workspace("smoke")
    staged_img, staged_audio = tg._stage_inputs(workspace, REF_IMG, REF_AUDIO)
    input_json, payload = tg._write_input_json(
        workspace, staged_img, staged_audio, tg.DEFAULT_TALKING_PROMPT)
    print("[smoke] 输入 JSON: {}".format(input_json))
    print("[smoke] prompt: {}".format(payload["prompt"][:60]))
    print("[smoke] 参考图: {} -> {}".format(REF_IMG, staged_img))
    print("[smoke] 音频: {} ({}s)".format(REF_AUDIO, 10))

    # 2) 构造 pipeline（走 int8 + 蒸馏，与 local-16g 档位一致）
    class _Args:
        def __init__(self):
            self.checkpoint_dir = checkpoint_dir
            self.use_int8 = True
            self.use_distill = True

    device = torch.device("cuda:0")
    t0 = time.time()
    pipe, model_type = build_pipeline(_Args(), device)
    print("[smoke] 模型加载完成 {:.0f}s，显存 {:.0f}MB".format(
        time.time() - t0, vram_mb()), flush=True)

    # 3) 音频嵌入
    import librosa
    import numpy as np
    speech, sr = librosa.load(staged_audio, sr=16000)
    full_emb = pipe.get_audio_embedding(
        speech, fps=25, device=device, sample_rate=sr, model_type=model_type)
    print("[smoke] 音频嵌入完成，shape={}，显存 {:.0f}MB".format(
        tuple(full_emb.shape), vram_mb()), flush=True)

    # 4) 生成（单段 93 帧，覆盖 10s 音频）
    from diffusers.utils import load_image
    image = load_image(staged_img)
    indices = torch.arange(2 * 2 + 1) - 2
    center = torch.arange(0, 93, 1).unsqueeze(1) + indices.unsqueeze(0)
    center = torch.clamp(center, min=0, max=full_emb.shape[0] - 1)
    audio_emb = full_emb[center][None, ...].to(device)

    generator = torch.Generator(device=device).manual_seed(42)
    t1 = time.time()
    peak = 0
    output, latent = pipe.generate_ai2v(
        image=image, prompt=payload["prompt"],
        negative_prompt="subtitles, style, blurred, low quality",
        resolution="480p", num_frames=93, num_inference_steps=8,
        text_guidance_scale=1.0, audio_guidance_scale=1.0,
        output_type="both", generator=generator, audio_emb=audio_emb,
        use_distill=True,
    )
    peak = torch.cuda.max_memory_allocated() / 2 ** 20
    print("[smoke] 生成完成 {:.0f}s，峰值显存 {:.0f}MB".format(
        time.time() - t1, peak), flush=True)

    # 5) 存视频（带音轨）
    import PIL.Image
    from longcat_video.audio_process.torch_utils import save_video_ffmpeg
    frames = output[0]
    video = [PIL.Image.fromarray((frames[i] * 255).astype("uint8"))
             for i in range(frames.shape[0])]
    out_tensor = torch.from_numpy(np.array(video))
    save_prefix = os.path.join(workspace, "smoke_out")
    save_video_ffmpeg(out_tensor, save_prefix, staged_audio, fps=25, quality=5)
    final = save_prefix + ".mp4"
    print("[smoke] 已保存 -> {}".format(final))
    print("[smoke] 大小 {:.1f} MB".format(os.path.getsize(final) / 2 ** 20))
    print("[smoke] 峰值显存 {:.0f}MB / 16303MB".format(peak))
    print("SMOKE_OK")


if __name__ == "__main__":
    main()
