"""光标空间定位与时间轴生成。

替换说明（2026-09，详见 docs/模型替换方案.md）
------------------------------------------------
UI-TARS-1.5-7B  ->  GUI-Owl-1.5-4B-Instruct
    - 原模型 bf16 需 16-18 GB，在 16 GB 卡上开箱 OOM；4.44B 版约 9-10 GB。
    - 许可证从 Apache-2.0（模型权重带额外条款）变为 MIT。
    - 共同点：都基于 Qwen-VL 系、都输出 **0-1000 归一化坐标**。
    - ⚠️ 不同点（务必注意）：**输出协议不一样**。原代码用 qwen-vl-utils 的
      ``parse_action_to_structure_output(model_type="qwen25vl")``，那是
      ``<|box_start|>(x,y)<|box_end|>`` 格式；而 GUI-Owl 走**工具调用协议**，
      输出 ``<tool_call>{"arguments":{"coordinate":[x,y]}}</tool_call>``，
      且必须在 system 消息里声明 "The screen's resolution is 1000x1000."。
      因此本文件的 grounding 辅助函数是**重写**而非复用，见 build_grounding_prompt。

whisperx large-v3 + align  ->  Qwen3-ASR-0.6B + Qwen3-ForcedAligner-0.6B

本文件同时修掉原实现的三个问题
------------------------------
1. **每句重载模型**：``infer_cursor()`` 原先把 ``pipeline(...)`` 写在函数体内，
   而该函数被 Pool 反复调用 —— 20 页 × 约 3 句 = 60 次重载 4.4B 模型。
   现改为模块级惰性单例。
2. **空 gpu_list 除零**：``--gpu_list`` 默认空串时 ``num_gpus = len("")`` 得 0，
   ``process_idx % num_gpus`` 直接 ZeroDivisionError。现统一走 pv_env.gpu_list() 兜底。
3. **`==` 写成赋值**：原版 ``cursor_result[i]["cursor"] == (w//2, h//2)`` 是恒假比较，
   "no" 指令的中心点回退从未生效。现改为真正的赋值。
"""

import os
import re
import json
import string
import subprocess
from os import path

import pv_env

GUI_OWL_REPO = "mPLUG/GUI-Owl-1.5-4B-Instruct"

# GUI-Owl 输出 0-1000 相对坐标（其 README 明确说明）
COORD_SCALE = 1000.0

_gui_model = None
_gui_processor = None


# ──────────────────────────────────────────────────────────────
# 模型加载
# ──────────────────────────────────────────────────────────────

def get_gui_model():
    """惰性加载 GUI-Owl。整个进程只加载一次。"""
    global _gui_model, _gui_processor
    if _gui_model is None:
        import torch
        from transformers import Qwen3VLForConditionalGeneration, AutoProcessor

        name = pv_env.model_path(GUI_OWL_REPO)
        device = "cuda:0" if torch.cuda.is_available() else "cpu"
        dtype = torch.bfloat16 if device.startswith("cuda") else torch.float32

        print("[cursor_gen] 加载 GUI-Owl: {} -> {}".format(name, device))
        _gui_model = Qwen3VLForConditionalGeneration.from_pretrained(
            name, torch_dtype=dtype, device_map=device,
        )
        _gui_processor = AutoProcessor.from_pretrained(name)
        _gui_model.eval()
    return _gui_model, _gui_processor


def release_gui_model():
    """释放 GUI-Owl，把显存让给数字人阶段。"""
    global _gui_model, _gui_processor
    import torch
    if _gui_model is not None:
        _gui_model = None
        _gui_processor = None
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
        print("[cursor_gen] 已释放 GUI-Owl")


# ──────────────────────────────────────────────────────────────
# 推理与坐标解析
# ──────────────────────────────────────────────────────────────

def build_grounding_prompt(instruction):
    """返回 (system_text, user_text)。

    ⚠️ 这里必须照搬 GUI-Owl 官方的**工具调用协议**，不能自创提示词。
    出处：X-PLUG/MobileAgent 的 ``grounding_and_kb/eval_grounding_benchmarks.py``
    中 ``only_two_action_system_prompt``。

    为什么不能自创：GUI-Owl 是在该模板上训练出来的 grounding 能力，模型靠
    "The screen's resolution is 1000x1000." 这一句才建立起输出 0-1000
    归一化坐标的约定，并且只在 <tool_call> 里吐 JSON 坐标。
    早先版本自创了一段"请输出 <point>x y</point>"的指令 —— 格式不在训练分布内，
    属于典型的"看起来合理但必然失败"的改法。
    """
    system_text = (
        "# Tools\n\n"
        "You may call one or more functions to assist with the user query.\n\n"
        "You are provided with function signatures within <tools></tools> XML tags:\n"
        "<tools>\n"
        '{"type": "function", "function": {"name": "computer_use", '
        '"description": "Use a mouse to interact with a computer.\\n'
        "* The screen's resolution is 1000x1000.\\n"
        "* Make sure to click any buttons, links, icons, etc with the cursor tip "
        "in the center of the element. Don't click boxes on their edges unless asked.\\n"
        "* don't use any other computer use tool like type, key, scroll, "
        "left_click_drag and so on.\\n"
        "* you can only use the left_click and mouse_move action to interact with "
        'the computer. if you can\'t find the element, you should terminate the task '
        'and report the failure.", "parameters": {"properties": {"action": '
        '{"description": "The action to perform. The available actions are:\\n'
        "* `mouse_move`: Move the cursor to a specified (x, y) pixel coordinate on the screen.\\n"
        "* `left_click`: Click the left mouse button with coordinate (x, y) pixel coordinate "
        'on the screen.", "enum": ["mouse_move", "left_click"], "type": "string"}, '
        '"coordinate": {"description": "(x, y): The x (pixels from the left edge) and y '
        "(pixels from the top edge) coordinates to move the mouse to. Required only by "
        '`action=mouse_move` and `action=left_click`.", "type": "array"}}, '
        '"required": ["action"], "type": "object"}}}\n'
        "</tools>\n\n"
        "For each function call, return a json object with function name and arguments "
        "within <tool_call></tool_call> XML tags:\n"
        "<tool_call>\n"
        '{"name": <function-name>, "arguments": <args-json-object>}\n'
        "</tool_call>\n"
    )
    return system_text, instruction


def parse_tool_call_point(response):
    """从 <tool_call> 的 JSON 里取 coordinate，返回 (x, y) 或 None。

    模型输出形如：
        <tool_call>
        {"name": "computer_use", "arguments": {"action": "left_click",
         "coordinate": [523, 418]}}
        </tool_call>
    优先走 JSON 解析（可靠），解析不出再用正则兜底。
    """
    import json as _json

    for block in re.findall(r"<tool_call>\s*(.*?)\s*</tool_call>", response, re.S):
        try:
            payload = _json.loads(block)
        except (ValueError, TypeError):
            continue
        arguments = payload.get("arguments") or {}
        coordinate = arguments.get("coordinate")
        if isinstance(coordinate, (list, tuple)) and len(coordinate) >= 2:
            try:
                return float(coordinate[0]), float(coordinate[1])
            except (TypeError, ValueError):
                pass
        # 有些输出把坐标写成 {"x":..,"y":..} 或字符串 "coordinate": "523 418"
        if isinstance(coordinate, dict) and "x" in coordinate and "y" in coordinate:
            try:
                return float(coordinate["x"]), float(coordinate["y"])
            except (TypeError, ValueError):
                pass
        if isinstance(coordinate, str):
            numbers = re.findall(r"-?\d+(?:\.\d+)?", coordinate)
            if len(numbers) >= 2:
                return float(numbers[0]), float(numbers[1])
    return None


def parse_point(response, img_width, img_height):
    """从模型输出解析出**像素**坐标，返回 ``(x, y)``；失败返回 ``None``。

    解析顺序（先精确后宽松）：
      1. ``<tool_call>`` 内的 JSON ``coordinate``（官方格式）
      2. ``(x, y)`` —— 官方 eval 脚本的首选正则
      3. ``[x, y]`` —— 官方 eval 脚本的兜底正则，也用于无 tool_call 包裹时

    坐标系：GUI-Owl 输出 0-1000 **相对**坐标（其 README 与系统提示均如此声明），
    此处换算成幻灯片图片的实际像素。若模型直接吐了像素值（> 1000）则原样采用。
    """
    raw = parse_tool_call_point(response)

    if raw is None:
        match = re.search(r"\(\s*(\d+(?:\.\d+)?)\s*,\s*(\d+(?:\.\d+)?)\s*\)", response)
        if match is None:
            match = re.search(r"\[\s*(\d+(?:\.\d+)?)\s*,\s*(\d+(?:\.\d+)?)\s*\]", response)
        if match is None:
            return None
        raw = (float(match.group(1)), float(match.group(2)))

    raw_x, raw_y = raw

    if raw_x <= COORD_SCALE and raw_y <= COORD_SCALE:
        x = raw_x / COORD_SCALE * img_width
        y = raw_y / COORD_SCALE * img_height
    else:
        x, y = raw_x, raw_y

    x = max(0.0, min(float(img_width - 1), x))
    y = max(0.0, min(float(img_height - 1), y))
    return (int(round(x)), int(round(y)))


def infer_cursor(instruction, image_path, device=None):
    """对单张幻灯片 + 单条指令做 grounding，返回 ``((x, y), token_text)``。"""
    import cv2
    import torch

    model, processor = get_gui_model()

    image = cv2.imread(str(image_path))
    if image is None:
        raise FileNotFoundError("找不到幻灯片图片: {}".format(image_path))
    img_h, img_w = image.shape[:2]

    system_text, user_text = build_grounding_prompt(instruction)

    # 必须带 system 消息：模型靠其中的 "resolution is 1000x1000" 建立坐标系约定，
    # 靠 <tool_call> 协议决定输出形态。官方 eval 脚本即为此结构。
    messages = [
        {"role": "system", "content": [{"type": "text", "text": system_text}]},
        {"role": "user", "content": [
            {"type": "image", "image": str(image_path)},
            {"type": "text", "text": user_text},
        ]},
    ]

    inputs = processor.apply_chat_template(
        messages, tokenize=True, add_generation_prompt=True,
        return_dict=True, return_tensors="pt",
    ).to(model.device)

    with torch.inference_mode():
        generated = model.generate(**inputs, max_new_tokens=256, do_sample=False)

    trimmed = generated[:, inputs.input_ids.shape[1]:]
    response = processor.batch_decode(
        trimmed, skip_special_tokens=True, clean_up_tokenization_spaces=False,
    )[0].strip()

    point = parse_point(response, img_w, img_h)
    if point is None:
        print("[cursor_gen] 坐标解析失败，指令={!r} 原始输出={!r}".format(instruction, response[:200]))
    return point, response


def _infer_task(task):
    """多进程 worker 入口（spawn 模式下需要顶层可 pickle 函数）。"""
    slide_idx, sentence_idx, prompt, cursor_prompt, image_path, gpu_id = task
    os.environ["CUDA_VISIBLE_DEVICES"] = str(gpu_id)
    point, token = infer_cursor(cursor_prompt, image_path)
    return {
        "slide": slide_idx, "sentence": sentence_idx, "speech_text": prompt,
        "cursor_prompt": cursor_prompt, "cursor": point, "token": token,
    }


def draw_red_dots_on_image(image_path, point, radius=5, out_path="output.jpg"):
    """调试辅助：把落点画在幻灯片上，便于肉眼核对 grounding 是否指偏。"""
    import cv2
    image = cv2.imread(image_path)
    if image is None:
        raise FileNotFoundError("Image not found at path: {}".format(image_path))
    cv2.circle(image, (int(point[0]), int(point[1])), radius, (0, 0, 255), thickness=-1)
    cv2.imwrite(out_path, image)


# ──────────────────────────────────────────────────────────────
# 时间轴
# ──────────────────────────────────────────────────────────────

def parse_script(script_text):
    pages = script_text.strip().split("###\n")
    result = []
    for page in pages:
        if not page.strip():
            continue
        lines = page.strip().split("\n")
        page_data = []
        for line in lines:
            if "|" not in line:
                continue
            text, cursor = line.split("|", 1)
            page_data.append([text.strip(), cursor.strip()])
        result.append(page_data)
    return result


def clean_text(text):
    text = text.lower()
    return text.translate(str.maketrans("", "", string.punctuation))


def get_audio_length(audio_path):
    """音频时长（秒）。优先 ffmpeg，缺失时回退 soundfile。"""
    try:
        result = subprocess.run(["ffmpeg", "-i", str(audio_path)],
                                stderr=subprocess.PIPE, text=True)
        for line in result.stderr.splitlines():
            if "Duration" in line:
                duration_str = line.split("Duration:")[1].split(",")[0].strip()
                hours, minutes, seconds = map(float, duration_str.split(":"))
                return hours * 3600 + minutes * 60 + seconds
    except FileNotFoundError:
        pass

    try:
        import soundfile as sf
        info = sf.info(str(audio_path))
        return float(info.frames) / float(info.samplerate)
    except Exception:
        return 0.0
    return 0.0


def timesteps(subtitles, aligned_result, audio_path):
    """把词级对齐结果折算成每句话的起止时间。

    ``aligned_result`` 需是 whisperx 兼容结构：
    ``{"segments": [{"words": [{"word":..,"start":..,"end":..}, ...]}]}``。
    Qwen3-ForcedAligner 的输出已由 ``speech_gen.align_with_qwen3`` 适配成该形状。
    """
    aligned_words_in_order = []
    for segment in aligned_result["segments"]:
        aligned_words_in_order.extend(segment["words"])

    if not aligned_words_in_order:
        return []

    aligned_words_num = len(aligned_words_in_order) - 1

    result = []
    current_idx = 0
    for sentence in subtitles:
        words_num = len(re.findall(r"\b\w+\b", sentence.lower()))
        start = aligned_words_in_order[min(aligned_words_num, current_idx)]["end"]

        current_idx += words_num
        end = aligned_words_in_order[min(aligned_words_num, current_idx)]["end"]

        result.append({"start": start, "end": end, "text": sentence})

    if result:
        result[0]["start"] = 0
        result[-1]["end"] = get_audio_length(audio_path)
    return result


# ──────────────────────────────────────────────────────────────
# 主流程
# ──────────────────────────────────────────────────────────────

def cursor_gen_per_sentence(script_path, slide_img_dir, slide_audio_dir,
                            cursor_save_path, gpu_list=None, language="English"):
    """逐句定位光标并生成时间轴，写入 cursor_save_path。

    单卡环境走「模型常驻 + 串行推理」；多卡才启用进程池。
    """
    from speech_gen import align_with_qwen3

    with open(script_path, "r", encoding="utf-8") as handle:
        script_with_cursor = "".join(handle.readlines())
    parsed_speech = parse_script(script_with_cursor)

    slide_imgs = [name for name in os.listdir(slide_img_dir)]
    slide_imgs = sorted(slide_imgs, key=lambda x: int(re.search(r"\d+", x).group()))
    slide_imgs = [path.join(slide_img_dir, name) for name in slide_imgs]

    if len(slide_imgs) < len(parsed_speech):
        raise ValueError("幻灯片数量({})少于脚本页数({})".format(len(slide_imgs), len(parsed_speech)))

    # 组装任务
    task_list = []
    for slide_idx in range(len(parsed_speech)):
        image_path = slide_imgs[slide_idx]
        for sentence_idx, (prompt, cursor_prompt) in enumerate(parsed_speech[slide_idx]):
            task_list.append((slide_idx, sentence_idx, prompt, cursor_prompt, image_path, 0))

    devices = pv_env.gpu_list(gpu_list)

    if len(devices) <= 1:
        # 单卡：模型常驻，避免每句重载
        print("[cursor_gen] 单卡串行模式，共 {} 条指令".format(len(task_list)))
        cursor_result = []
        for idx, task in enumerate(task_list):
            cursor_result.append(_infer_task(task))
            if (idx + 1) % 10 == 0:
                print("[cursor_gen] {}/{}".format(idx + 1, len(task_list)))
    else:
        import multiprocessing as mp
        print("[cursor_gen] 多卡模式 {}，共 {} 条指令".format(devices, len(task_list)))
        for idx, task in enumerate(task_list):
            task_list[idx] = task[:5] + (devices[idx % len(devices)],)
        ctx = mp.get_context("spawn")
        with ctx.Pool(processes=len(devices)) as pool:
            cursor_result = pool.map(_infer_task, task_list)

    # 「no」指令与解析失败 -> 画面中心
    import cv2
    slide_h, slide_w = cv2.imread(slide_imgs[0]).shape[:2]
    center = (slide_w // 2, slide_h // 2)
    for item in cursor_result:
        if item["cursor"] is None:
            item["cursor"] = center

    cursor_token = "".join(item["token"] for item in cursor_result)

    # grounding 已全部完成。GUI-Owl 占约 9-10 GB，而下一步的强制对齐还要加载
    # 一个 1.8 GB 的模型。在 16 GB 卡上（系统本身已占 6-7 GB），两者叠加会 OOM，
    # 因此这里显式释放 GUI-Owl —— 这是「能跑」与「爆显存」的分界点。
    release_gui_model()

    # ── 时间轴 ──
    slide_sentence_timesteps = []
    slide_audio = [name for name in os.listdir(slide_audio_dir) if name.lower().endswith(".wav")]
    slide_audio = sorted(slide_audio, key=lambda x: int(re.search(r"\d+", x).group()))
    slide_audio = [path.join(slide_audio_dir, name) for name in slide_audio]

    for idx, slide_audio_path in enumerate(slide_audio):
        raw_texts, subtitle, cursor = [], [], []
        for info in cursor_result:
            if info["slide"] == idx:
                raw_texts.append(info["speech_text"])
                subtitle.append(clean_text(info["speech_text"]))
                cursor.append(info["cursor"])

        if not raw_texts:
            slide_sentence_timesteps.append([])
            continue

        # 用**原始文本**做强制对齐（clean_text 去掉了标点与大小写，会伤对齐精度）
        aligned = align_with_qwen3(slide_audio_path, " ".join(raw_texts), lang=language)
        sentence_timesteps = timesteps(subtitle, aligned, slide_audio_path)

        # 句数可能因词数统计偏差而对不齐，取较短的一侧做保护性配对
        if len(sentence_timesteps) != len(cursor):
            print("[cursor_gen] 第 {} 页句数不匹配：时间轴 {} vs 光标 {}".format(
                idx, len(sentence_timesteps), len(cursor)))

        for pos in range(min(len(sentence_timesteps), len(cursor))):
            sentence_timesteps[pos]["cursor"] = cursor[pos]
            # 同时保留原文：sentence_timesteps 里的 text 是 clean_text 的产物
            # （全小写、去标点），用于字幕显示不合适。
            sentence_timesteps[pos]["raw_text"] = raw_texts[pos]

        slide_sentence_timesteps.append(sentence_timesteps)

    # 拼接为全局时间轴
    start_time_now = 0.0
    new_slide_sentence_timesteps = []
    for sentence_timesteps in slide_sentence_timesteps:
        duration = 0.0
        for idx in range(len(sentence_timesteps)):
            if sentence_timesteps[idx]["start"] is None:
                sentence_timesteps[idx]["start"] = sentence_timesteps[idx - 1]["end"]
            if sentence_timesteps[idx]["end"] is None:
                sentence_timesteps[idx]["end"] = sentence_timesteps[idx + 1]["start"]

        for idx in range(len(sentence_timesteps)):
            sentence_timesteps[idx]["start"] += start_time_now
            sentence_timesteps[idx]["end"] += start_time_now
            duration += sentence_timesteps[idx]["end"] - sentence_timesteps[idx]["start"]

        start_time_now += duration
        new_slide_sentence_timesteps.extend(sentence_timesteps)

    with open(cursor_save_path.replace(".json", "_mid.json"), "w", encoding="utf-8") as handle:
        json.dump(cursor_result, handle, indent=2)
    with open(cursor_save_path, "w", encoding="utf-8") as handle:
        json.dump(new_slide_sentence_timesteps, handle, indent=2)

    return len(cursor_token) / 4
