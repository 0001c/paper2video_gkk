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
import time
from os import path
from pathlib import Path

import pv_env

# 结构化进度（P1）：可选增强，导入失败绝不能拖垮流水线（见 progress_report.py）。
# 光标阶段**特别需要**它：见下面单卡循环里的说明（18 条回退指令时，
# 界面原先对整个阶段一无所知）。
try:
    import progress_report
except Exception:                      # pragma: no cover
    progress_report = None

GUI_OWL_REPO = "mPLUG/GUI-Owl-1.5-4B-Instruct"

# GUI-Owl 输出 0-1000 相对坐标（其 README 明确说明）
COORD_SCALE = 1000.0

# grounding 用的图片长边上限（像素）。
# pipeline_light 按 400 dpi 渲染幻灯片（2016x1512）是为了**成片清晰度**，但 grounding
# 并不需要那个分辨率：模型只回 0-1000 归一化坐标，分辨率越高，视觉 token 数按面积
# 增长（2016x1512 约 3.9k visual token），在 16 GB 卡上会把显存顶到 ~97% 并触发
# 显存颠簸（实测每句从 2s 退化到 ~80s）。这里给模型一份缩过的副本，
# **坐标仍按原图尺寸换算**（见 infer_cursor 里 parse_point 的入参），故落点精度不变。
GROUNDING_MAX_EDGE = 1008

_gui_model = None
_gui_processor = None


def prepare_grounding_image(image_path):
    """返回「该喂给模型」的图片路径：超过 GROUNDING_MAX_EDGE 才缩放，否则原图。

    副本落在 TMP_DIR（纯英文路径），并按源文件名缓存，同一页的多句复用同一份。
    """
    from PIL import Image

    with Image.open(image_path) as handle:
        width, height = handle.size
        scale = GROUNDING_MAX_EDGE / float(max(width, height))
        if scale >= 1.0:
            return str(image_path)
        target = (max(1, int(round(width * scale))),
                  max(1, int(round(height * scale))))
        resized = handle.convert("RGB").resize(target, Image.LANCZOS)

    os.makedirs(pv_env.TMP_DIR, exist_ok=True)
    # 文件名带上目标尺寸：否则调整 GROUNDING_MAX_EDGE 后会命中上一次的旧副本
    stem = path.splitext(path.basename(image_path))[0]
    out_path = path.join(pv_env.TMP_DIR, "grounding_{}_{}x{}.png".format(
        stem, target[0], target[1]))
    if not path.exists(out_path):
        resized.save(out_path)
    return out_path


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

    # ⚠️ 这里**不要**用 cv2.imread 取尺寸：OpenCV 走 Windows 的 ANSI API，
    # 读不了含中文的路径（实测报 `cv::findDecoder ... can't open/read file`，
    # 随后被下面的 FileNotFoundError 抛出）。生产链路目前只是因为
    # `--result_dir` 传的是**相对路径**（字符串本身是纯 ASCII，由系统按 cwd
    # 解析）才没暴露；一旦有人传绝对路径 `E:\Development\26年\...`，光标定位会
    # 直接崩在这一行。PIL 内部用宽字符 API，中英路径都能读。
    from PIL import Image as _Image

    try:
        with _Image.open(str(image_path)) as handle:
            img_w, img_h = handle.size
    except FileNotFoundError:
        raise FileNotFoundError("找不到幻灯片图片: {}".format(image_path))

    # 真图只用来取尺寸；喂给模型的是缩小版（见 prepare_grounding_image 的说明）。
    model_image_path = prepare_grounding_image(image_path)

    system_text, user_text = build_grounding_prompt(instruction)

    # 必须带 system 消息：模型靠其中的 "resolution is 1000x1000" 建立坐标系约定，
    # 靠 <tool_call> 协议决定输出形态。官方 eval 脚本即为此结构。
    messages = [
        {"role": "system", "content": [{"type": "text", "text": system_text}]},
        {"role": "user", "content": [
            {"type": "image", "image": model_image_path},
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

    # ⚠️ 这里必须传**原图**宽高：模型给的是 0-1000 归一化坐标，
    # 换算回像素要按成片实际分辨率（2016x1512）来，传缩小版尺寸会让光标偏到左上角。
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
# PDF 文本定位（不依赖 grounding 模型）
# ──────────────────────────────────────────────────────────────

_TEXT_KEEP = re.compile(r"[0-9a-z\u4e00-\u9fff]+")


def normalize_text(text):
    """文本规范化：小写、只留字母数字与汉字、压空白。

    中英通用 —— 比 ``\\b\\w+\\b`` 可靠（后者对中文等于没分词）。
    """
    return " ".join(_TEXT_KEEP.findall(str(text).lower()))


def extract_quote(description):
    """取出描述里**引号包裹**的短语；没有引号则返回 ``None``。

    新 prompt 要求描述形如 ``... | "Slide Builder"``（逐字引用幻灯片原文），
    这里把引号内容取出用于精确定位。同时兼容中英文引号。
    """
    if not description:
        return None
    for pattern in (r'"([^"]{2,160})"',
                    "\u201c([^\u201d]{2,160})\u201d",
                    r"'([^']{2,160})'"):
        match = re.search(pattern, description)
        if match:
            return match.group(1).strip()
    return None


def strip_cursor_prefix(description):
    """去掉 ``cursor on the`` 一类前缀，留下目标描述。

    这是为了兼容**改 prompt 之前**的旧描述（概念化表述）—— 让它们至少有机会
    命中，而不是一律回退到 grounding 模型。
    """
    text = re.sub(r"^\s*cursor\s+(on|to|at)\s+(the\s+)?", "",
                  str(description or ""), flags=re.I)
    return text.strip(" .\u3002\uff0c,\"'")


class PdfSlideLocator:
    """用幻灯片 PDF 的**文本坐标**定位短语 —— 不经过任何神经网络。

    为什么需要它（都是实测结论）：

    * **GUI-Owl 是 UI grounding 模型**。它的 system prompt（照搬官方工具调用
      协议，不能自创）明确要求「click any **buttons, links, icons**」，而幻灯片
      上根本没有这类控件。实测它会给出「区域级」答案：`the presentation icon`
      与 `the method overview section` 两条不同描述落到相距仅 73px 的位置；
      页8 的 4 条描述落点 y 全挤在同一条水平带上。
    * **提高喂图分辨率没有用**：GROUNDING_MAX_EDGE 1008 -> 2016 后，落点距
      目标反而更远（70.8pt -> 129.3pt）。瓶颈是模型的跨域行为，不是清晰度。

    而 LaTeX 编译出的 ``slides.pdf`` 里，**每一行文字的坐标都是精确已知的**。
    只要描述能给出「幻灯片上的原文短语」，就能直接查到位置：像素级精度、
    零推理开销、完全可解释、可离线复现（无随机性）。

    查不到时不报错，由调用方回退 GUI-Owl —— 所以它是**增强**而非替换。
    """

    def __init__(self, pdf_path, img_width, img_height):
        import fitz

        self.doc = fitz.open(pdf_path)
        self.img_width = int(img_width)
        self.img_height = int(img_height)
        self._lines_cache = {}
        self._page_cache = {}

    @property
    def page_count(self):
        return self.doc.page_count

    def _page(self, page_idx):
        if page_idx not in self._page_cache:
            self._page_cache[page_idx] = self.doc.load_page(page_idx)
        return self._page_cache[page_idx]

    def _lines(self, page_idx):
        """该页的文字行 ``[(bbox, text)]``。

        取**行级**而非块级：块往往是整段甚至整栏，落点会落在段落中心；
        行级才能指到「正在讲的那一行」。
        """
        if page_idx not in self._lines_cache:
            page = self._page(page_idx)
            lines = []
            for block in page.get_text("dict").get("blocks", []):
                if block.get("type") != 0:          # 0 = 文本块
                    continue
                for line in block.get("lines", []):
                    text = "".join(span.get("text", "")
                                   for span in line.get("spans", []))
                    if text.strip():
                        lines.append((line["bbox"], text))
            self._lines_cache[page_idx] = lines
        return self._lines_cache[page_idx]

    def _to_pixels(self, page, bbox):
        """PDF 点 -> 成片像素。按**原始渲染尺寸**换算（400dpi 下 2016x1512）。"""
        sx = self.img_width / page.rect.width
        sy = self.img_height / page.rect.height
        x0, y0, x1, y1 = bbox
        return (int(round((x0 + x1) / 2 * sx)), int(round((y0 + y1) / 2 * sy)))

    def locate(self, page_idx, phrase, min_ratio=0.75):
        """在 ``page_idx`` 页定位短语，返回像素坐标；失败返回 ``None``。

        三级策略，由精确到宽松：
          ① ``page.search_for`` —— 原文完全一致时最准
          ② 行级「规范化包含」—— 容忍大小写与标点差异
          ③ 行级模糊匹配（difflib ``>= min_ratio``）
        """
        phrase = str(phrase or "").strip().strip("\"'")
        if not phrase or page_idx < 0 or page_idx >= self.doc.page_count:
            return None

        page = self._page(page_idx)

        try:
            rects = page.search_for(phrase)
        except Exception:
            rects = []
        if rects:
            first = rects[0]
            return self._to_pixels(page, (first.x0, first.y0, first.x1, first.y1))

        target = normalize_text(phrase)
        if not target:
            return None

        hits = [(bbox, text) for bbox, text in self._lines(page_idx)
                if target in normalize_text(text)]
        if hits:
            # 多行都包含时取**最短**的那行：最贴近原短语，避免落到整段中心
            bbox, _ = min(hits, key=lambda item: len(normalize_text(item[1])))
            return self._to_pixels(page, bbox)

        import difflib

        best_bbox, best_ratio = None, 0.0
        for bbox, text in self._lines(page_idx):
            ratio = difflib.SequenceMatcher(
                None, target, normalize_text(text)).ratio()
            if ratio > best_ratio:
                best_bbox, best_ratio = bbox, ratio
        if best_bbox is not None and best_ratio >= min_ratio:
            return self._to_pixels(page, best_bbox)
        return None

    def close(self):
        try:
            self.doc.close()
        except Exception:
            pass


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
    """优先读取音频帧数，避免逐页启动进程，也保留完整精度。"""
    try:
        import soundfile as sf
        info = sf.info(str(audio_path))
        return float(info.frames) / float(info.samplerate)
    except Exception:
        pass
    try:
        result = subprocess.run(["ffmpeg", "-i", str(audio_path)],
                                stderr=subprocess.PIPE, text=True)
        for line in result.stderr.splitlines():
            if "Duration" in line:
                duration_str = line.split("Duration:")[1].split(",")[0].strip()
                hours, minutes, seconds = map(float, duration_str.split(":"))
                return hours * 3600 + minutes * 60 + seconds
    except OSError:
        pass
    return 0.0


def content_chars(text):
    """提取「有效字符」：字母、数字、汉字（``str.isalnum()`` 对汉字为真）。

    这是给**对齐单元**与**句子**建立同一计量口径用的。

    为什么不能沿用 ``re.findall(r"\\b\\w+\\b", ...)`` 数长度：``\\b`` 是**词边界**，
    而中文不写空格 —— 一整串汉字只会命中一次。实测同一页的三句话，
    ``\\w+`` 给出 3 / 9 / 3，而它们的有效字符是 36 / 51 / 46，
    **差了一个数量级**。详见 timesteps 的说明。
    """
    return [char for char in str(text).lower() if char.isalnum()]


def timesteps(subtitles, aligned_result, audio_path):
    """把词级对齐结果折算成每句话的起止时间。

    ``aligned_result`` 需是 whisperx 兼容结构：
    ``{"segments": [{"words": [{"word":..,"start":..,"end":..}, ...]}]}``。
    Qwen3-ForcedAligner 的输出已由 ``speech_gen.align_with_qwen3`` 适配成该形状。

    ⚠️ 这里**不能**按「英文词数」切分中文（修复前的老写法）::

        words_num = len(re.findall(r"\\b\\w+\\b", sentence.lower()))

    中文没有词边界，一句话会被算成 1~3 个"词"，而 Qwen3-ForcedAligner 对中文
    输出的是**逐字**时间戳（89 个单元 / 133 个有效字符）。于是 ``current_idx``
    推进得极慢，每句都只能从"音频开头附近"取值：

        句0    0.08 ->   0.56   ( 0.48s)   ← 真实 36 字
        句1    0.56 ->   3.20   ( 2.64s)   ← 真实 51 字
        句2    3.20 ->  19.92   (16.72s)   ← 由末尾那句 `result[-1]["end"] = 音频总长` 兜底独占 84%

    后果不是"精度差一点"，而是**光标在错误的时间停在错误的位置**：开头几句一闪
    而过（顺序仍对，看着像"准"），随后长时间钉在末句的坐标上 —— 而末句常讲流程图 /
    图表，观感就成了"总跑去指图片"。

    改为把对齐单元展开成**字符级时间轴**，再按句子的有效字符数单调切分：

    * 中英同口径（都是字符），不再依赖分词
    * 保留对齐器给出的真实语速与停顿分布（不是按字数均分整段音频）
    * 单调递增、句间无缝

    实测前提：对齐侧与句子侧的有效字符数**完全相等**（第 0 页 133 == 133）。
    偏差过大时会打印告警，便于上游文本口径变化时及时发现。
    """
    words = []
    for segment in aligned_result.get("segments", []):
        words.extend(segment.get("words", []))

    if not words:
        return []

    # ── ① 展开成字符级时间点：每个有效字符拿到自己的结束时刻 ──
    # 英文词（如 "Paper2Video"）是一个对齐单元，但含多个字符 —— 在它的
    # start/end 之间按字符数均匀插值，这样"字符"才成为中英统一的计量单位。
    char_times = []
    for word in words:
        text = str(word.get("word", ""))
        start = float(word.get("start") or 0.0)
        end = float(word.get("end") or start)
        if end < start:
            end = start
        chars = content_chars(text)
        if not chars:
            continue
        span = end - start
        count = len(chars)
        for offset in range(count):
            char_times.append(start + span * (offset + 1) / count)

    if not char_times:
        return []

    # ── ② 按有效字符数切分句子 ──
    lengths = [max(1, len(content_chars(sentence))) for sentence in subtitles]
    total_chars = len(char_times)
    total_expected = sum(lengths)

    if abs(total_chars - total_expected) > max(3, 0.02 * total_expected):
        print("[cursor_gen] ⚠️ 对齐字符数({}) 与讲稿字符数({}) 偏差较大，"
              "时间轴将按比例分配".format(total_chars, total_expected))

    result = []
    pos = 0
    for sentence, length in zip(subtitles, lengths):
        start_pos = pos
        pos = min(pos + length, total_chars)
        start_time = 0.0 if start_pos == 0 else char_times[start_pos - 1]
        end_time = char_times[pos - 1]
        result.append({"start": round(start_time, 3), "end": round(end_time, 3),
                       "text": sentence})

    if result:
        result[0]["start"] = 0.0
        # 末句：对齐器可能漏掉尾部静音，**只在明显短于音频时**才补齐。
        # 老实现是无条件 `result[-1]["end"] = get_audio_length(...)`，
        # 那正是"末句独吞时长"的直接成因之一 —— 它会把前面所有句子的
        # 错误从"偏短"放大成"末句吃掉 80%+"。
        audio_len = get_audio_length(audio_path)
        if audio_len and result[-1]["end"] < audio_len - 0.3:
            result[-1]["end"] = round(audio_len, 3)

    return result


# ──────────────────────────────────────────────────────────────
# 主流程
# ──────────────────────────────────────────────────────────────

def _open_pdf_locator(pdf_path, img_width, img_height, page_count):
    """打开 PDF 定位器，并**校验页数 == 幻灯片张数**。

    校验是必需的：pipeline 会把 ``--paper_latex_root`` **整个目录**复制到
    ``result/<run>/latex_proj``，而那个目录里可能自带一个 ``slides.pdf``
    （上游 demo 的示例，16 页）。如果本次没跑 stage 1、只是复用了已有的
    ``slide_imgs``，这个 PDF 就与幻灯片**不同源** —— 拿它定位会**静默错位**，
    比不定还糟（实测踩过：用示例 PDF 当 ground truth，把描述匹配率算成 14%，
    换成同源 PDF 后是 45%）。

    所以宁可退回 grounding，也不要错位。
    """
    if not pdf_path:
        print("[cursor_gen] 未提供幻灯片 PDF，跳过 PDF 文本定位（全部走 grounding）")
        return None
    if not path.isfile(pdf_path):
        print("[cursor_gen] 幻灯片 PDF 不存在，跳过 PDF 文本定位：{}".format(pdf_path))
        return None
    try:
        locator = PdfSlideLocator(pdf_path, img_width, img_height)
    except Exception as exc:
        print("[cursor_gen] 打开 PDF 失败，跳过 PDF 文本定位：{}".format(exc))
        return None
    if locator.page_count != page_count:
        print("[cursor_gen] ⚠️ PDF 页数({}) != 幻灯片张数({})，对应关系不可信，"
              "放弃 PDF 定位以免错位；请确认该 PDF 与 slide_imgs 来自同一次编译"
              .format(locator.page_count, page_count))
        locator.close()
        return None
    print("[cursor_gen] PDF 文本定位已启用：{}（{} 页）".format(pdf_path, page_count))
    return locator


def cursor_gen_per_sentence(script_path, slide_img_dir, slide_audio_dir,
                            cursor_save_path, gpu_list=None, language="English",
                            slide_pdf_path=None, resume_cache=False, timing_source='local_forced_aligner', model_snapshot=None):
    """逐句定位光标并生成时间轴，写入 cursor_save_path。

    定位分两条路，**优先 PDF 文本坐标**（像素级精确、零推理开销、可离线复现），
    查不到才回退 GUI-Owl grounding。理由见 :class:`PdfSlideLocator` 的说明。

    单卡环境走「模型常驻 + 串行推理」；多卡才启用进程池。
    """
    from speech_gen import align_with_qwen3
    import course
    api_grounding = bool(model_snapshot and model_snapshot.get('model_config_origin') == 'v2' and model_snapshot['model_config']['stages']['grounding']['mode'] == 'api')
    grounding_identity = None
    if api_grounding:
        from model_runtime import role_settings
        from bailian_grounding import PROTOCOL, infer
        settings = role_settings(model_snapshot, 'grounding')
        grounding_identity = {'model': settings['model_type'], 'provider': settings['provider'], 'scope': settings['scope_id'],
                              'connection': settings['connection_id'], 'endpoint': settings['url'], 'protocol': PROTOCOL,
                              'pdf': course.digest(Path(slide_pdf_path).read_bytes()) if slide_pdf_path and path.isfile(slide_pdf_path) else None}
    def point_key(task):
        identity = {'v': 1, 'image': course.digest(Path(task[4]).read_bytes()), 'cursor': task[3]}
        if grounding_identity is not None:
            identity.update(v=2, grounding=grounding_identity)
        return course.digest(identity)
    cache_path = cursor_save_path.replace('.json', '_cache.json')
    cache = course.read_json(cache_path, {}) if resume_cache else {}
    points = cache.setdefault('points', {})
    alignments = cache.setdefault('alignments', {})

    with open(script_path, "r", encoding="utf-8") as handle:
        script_with_cursor = "".join(handle.readlines())
    parsed_speech = parse_script(script_with_cursor)

    slide_imgs = [name for name in os.listdir(slide_img_dir) if name.lower().endswith('.png')]
    slide_imgs = sorted(slide_imgs, key=lambda x: int(re.search(r"\d+", x).group()))
    slide_imgs = [path.join(slide_img_dir, name) for name in slide_imgs]

    if len(slide_imgs) < len(parsed_speech):
        raise ValueError("幻灯片数量({})少于脚本页数({})".format(len(slide_imgs), len(parsed_speech)))

    # 幻灯片渲染尺寸：PDF 坐标换算与「中心点兜底」都要用。
    # 同样避开 cv2.imread：它读不了含中文路径（理由见 infer_cursor）。
    from PIL import Image as _Image

    with _Image.open(str(slide_imgs[0])) as _handle:
        slide_w, slide_h = _handle.size

    # 组装任务
    task_list = []
    for slide_idx in range(len(parsed_speech)):
        image_path = slide_imgs[slide_idx]
        for sentence_idx, (prompt, cursor_prompt) in enumerate(parsed_speech[slide_idx]):
            task_list.append((slide_idx, sentence_idx, prompt, cursor_prompt, image_path, 0))

    # ── 第一级：PDF 文本坐标 ──
    resolved = [None] * len(task_list)
    point_sources = [None] * len(task_list)
    point_keys = [point_key(task) for task in task_list] if resume_cache else []
    if resume_cache:
        for pos, key in enumerate(point_keys):
            resolved[pos] = points.get(key)
            if resolved[pos] is not None:
                point_sources[pos] = 'cache'
    locator = _open_pdf_locator(slide_pdf_path, slide_w, slide_h, len(parsed_speech))
    if locator is not None:
        for pos, task in enumerate(task_list):
            if resolved[pos] is not None:
                continue
            slide_idx, _, _, cursor_prompt, _, _ = task
            phrase = extract_quote(cursor_prompt) or strip_cursor_prefix(cursor_prompt)
            point = locator.locate(slide_idx, phrase)
            if point is not None:
                resolved[pos] = point
                point_sources[pos] = 'pdf'
        hit = sum(1 for item in resolved if item is not None)
        print("[cursor_gen] PDF 文本定位命中 {}/{} 条（其余回退 GUI-Owl）".format(
            hit, len(task_list)))
        locator.close()

    # ── 第二级：GUI-Owl grounding，只处理 PDF 没命中的 ──
    devices = pv_env.gpu_list(gpu_list)
    pending = [task for pos, task in enumerate(task_list) if resolved[pos] is None]

    if not pending:
        print("[cursor_gen] 全部由缓存/PDF定位完成，未调用 grounding 模型")
        fallback = []
    elif api_grounding:
        fallback = []
        for index, task in enumerate(pending, 1):
            point, token = infer(task[3], task[4], model_snapshot)
            fallback.append({'slide': task[0], 'sentence': task[1], 'speech_text': task[2],
                             'cursor_prompt': task[3], 'cursor': point, 'token': token})
            if resume_cache:
                points[point_key(task)] = point if point is not None else [slide_w // 2, slide_h // 2]
                course.write_json(cache_path, cache)
            if progress_report:
                progress_report.report('cursor', stage=2, done=index, total=len(pending))
    elif len(devices) <= 1:
        # 单卡：模型常驻，避免每句重载
        print("[cursor_gen] 单卡串行模式，共 {} 条指令".format(len(pending)))
        fallback = []
        total_pending = len(pending)
        for idx, task in enumerate(pending):
            item_started = time.time()
            item = _infer_task(task)
            fallback.append(item)
            if resume_cache and item.get('cursor') is not None:
                key = point_key(task)
                points[key] = item['cursor']
                course.write_json(cache_path, cache)
            cost = time.time() - item_started
            # ⚠️ 逐条打印 + 上报，**不能只在每 10 条时打印一次**（2026-09-19 实测）：
            # 一次真实任务里有 18 条回退指令，日志只留下 "10/18" 一行，
            # 而其中**某一条花了 6 分半**（其余约 30 秒）—— 于是那 6 分半里
            # 日志完全不动，界面也只有"光标定位"四个字，
            # 使用者合理地判断成"卡死了"（我们自己也差点这么判断）。
            # 逐条打耗时之后，慢的是哪一条、慢了多久，事后一眼可查。
            print("[cursor_gen] {}/{} 完成，本条 {:.1f} 秒".format(
                idx + 1, total_pending, cost))
            if progress_report:
                try:
                    progress_report.report("cursor", stage=2, done=idx + 1,
                                           total=total_pending, page=task[0])
                except Exception:
                    pass
    else:
        import multiprocessing as mp
        print("[cursor_gen] 多卡模式 {}，共 {} 条指令".format(devices, len(pending)))
        pending = [task[:5] + (devices[i % len(devices)],)
                   for i, task in enumerate(pending)]
        ctx = mp.get_context("spawn")
        with ctx.Pool(processes=len(devices)) as pool:
            fallback = pool.map(_infer_task, pending)

    # 合回原始顺序，并标注来源（便于统计两条路各占多少）
    cursor_result = []
    fallback_iter = iter(fallback)
    for pos, task in enumerate(task_list):
        slide_idx, sentence_idx, prompt, cursor_prompt, _, _ = task
        if resolved[pos] is not None:
            cursor_result.append({
                "slide": slide_idx, "sentence": sentence_idx, "speech_text": prompt,
                "cursor_prompt": cursor_prompt, "cursor": resolved[pos],
                "token": "", "source": point_sources[pos],
            })
        else:
            item = next(fallback_iter)
            item["source"] = "bailian_grounding" if api_grounding else "grounding"
            cursor_result.append(item)

    # 「no」指令与解析失败 -> 画面中心
    center = (slide_w // 2, slide_h // 2)
    for item in cursor_result:
        if item["cursor"] is None:
            item["cursor"] = center
    if resume_cache:
        for key, item in zip(point_keys, cursor_result):
            points[key] = item['cursor']
        course.write_json(cache_path, cache)

    cursor_token = "".join(item["token"] for item in cursor_result)

    # grounding 已全部完成。GUI-Owl 占约 9-10 GB，而下一步的强制对齐还要加载
    # 一个 1.8 GB 的模型。在 16 GB 卡上（系统本身已占 6-7 GB），两者叠加会 OOM，
    # 因此这里显式释放 GUI-Owl —— 这是「能跑」与「爆显存」的分界点。
    if not api_grounding:
        release_gui_model()

    # ── 时间轴 ──
    slide_sentence_timesteps = []
    slide_audio = [name for name in os.listdir(slide_audio_dir) if name.lower().endswith(".wav")]
    slide_audio = sorted(slide_audio, key=lambda x: int(re.search(r"\d+", x).group()))
    if len(slide_audio) < len(parsed_speech):
        raise ValueError('页音频尚未完整生成，不能生成光标时间轴')
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
        api_alignment = None
        if timing_source == 'tts_timestamp':
            from bailian_speech import read_pair, plain_text
            pcm, api_alignment = read_pair(slide_audio_path)
            if plain_text(api_alignment['text']) != plain_text(' '.join(raw_texts)):
                raise ValueError('时间戳原稿与光标稿不一致，禁止回退本地对齐')
        alignment_identity = {'v': 1, 'audio': course.digest(Path(slide_audio_path).read_bytes()), 'texts': raw_texts, 'cursor': cursor, 'language': language}
        if api_alignment is not None:
            alignment_identity.update(v=3, source=timing_source, timing=api_alignment)
        alignment_key = course.digest(alignment_identity) if resume_cache else None
        cached_alignment = alignments.get(alignment_key)
        if cached_alignment is not None:
            sentence_timesteps = [dict(item) for item in cached_alignment]
        else:
            aligned = api_alignment if api_alignment is not None else align_with_qwen3(slide_audio_path, " ".join(raw_texts), lang=language)
            if api_alignment is not None:
                from bailian_grounding import timestamp_sentences
                sentence_timesteps = timestamp_sentences(raw_texts, aligned, get_audio_length(slide_audio_path))
            else:
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
        if resume_cache:
            alignments[alignment_key] = [dict(item) for item in sentence_timesteps]
            course.write_json(cache_path, cache)

    # 拼接为全局时间轴
    start_time_now = 0.0
    new_slide_sentence_timesteps = []
    for page_idx, sentence_timesteps in enumerate(slide_sentence_timesteps):
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

        # 页间时间轴必须包含静音和停顿，长课不能累计丢掉这些时长。
        start_time_now += get_audio_length(slide_audio[page_idx])
        new_slide_sentence_timesteps.extend(sentence_timesteps)

    with open(cursor_save_path.replace(".json", "_mid.json"), "w", encoding="utf-8") as handle:
        json.dump(cursor_result, handle, indent=2)
    with open(cursor_save_path, "w", encoding="utf-8") as handle:
        json.dump(new_slide_sentence_timesteps, handle, indent=2)

    return len(cursor_token) / 4
