import re
import time
import pdb
import os, sys
from os import path
from PIL import Image
from pathlib import Path
from camel.models import ModelFactory
from camel.agents import ChatAgent
from camel.messages import BaseMessage
from camel.types import ModelPlatformType
from wei_utils import get_agent_config


def validate_script_output(text, expected_slides):
    """校验 VLM 产出的讲稿是否**结构完整**。返回 ``(是否合格, 问题说明)``。

    为什么不能只判空：实测踩到过 —— VLM 返回了一个**非空但被截断**的响应，
    末尾停在 ``... | "Paper``（引号没闭合），而当时只判了空，于是截断结果被
    当成正常讲稿写盘：12 页讲稿只剩 8 页、TTS 只合成 8 段音频，一直到最后
    出片才发现少了一半内容。**非空 ≠ 完整**。

    三项结构性检查（都很便宜，且与语言无关）：

    * **引号成对** —— 新 prompt 要求光标目标写成 ``"幻灯片原文"``，截断时
      最容易表现为引号落单；
    * **页数不少于幻灯片张数** —— 漏页是最典型的截断形态；
    * **每页至少有一行 ``讲稿 | 目标``** —— 防止出现空页。
    """
    issues = []
    stripped = str(text).strip()

    if stripped.count('"') % 2 != 0:
        issues.append("引号不成对（疑似截断）")

    pages = [page for page in stripped.split("###") if page.strip()]
    if len(pages) < expected_slides:
        issues.append("页数 {} 少于幻灯片张数 {}".format(len(pages), expected_slides))

    blank = [idx + 1 for idx, page in enumerate(pages)
             if not any("|" in line for line in page.splitlines())]
    if blank:
        issues.append("第 {} 页没有任何「讲稿 | 目标」行".format(blank))

    return (not issues), "；".join(issues)


def subtitle_cursor_gen(slide_imgs_dir, prompt_path, model_config, retries=None):
    """让 VLM 看全部幻灯片，产出「讲稿 | 光标目标」的逐页文本。

    ⚠️ 为什么必须重试 + 判空 + **判完整性**
    ---------------------------------------
    实测 VLM **会偶发返回空响应**，camel 只打一行 warning：

        camel.camel.agents.chat_agent - WARNING - No messages returned in
        `step()`. The model returned an empty response.

    此时 ``response.msg`` 是 ``None``。原实现直接 ``response.msg.content``
    会在 ``AttributeError: 'NoneType' object has no attribute 'content'`` 上崩掉
    —— **一次 API 抖动就废掉整条流水线**，而这时前面的幻灯片生成已经跑完
    （可能花了几分钟和若干 token），后面更贵的 TTS + 光标定位全部白费。

    更隐蔽的是**非空但被截断**：实测拿到过一份只写到第 8 页、末尾引号未闭合的
    响应（12 页幻灯片）。只判空的话它会一路带到成片里。因此这里对非空响应
    再做一次结构校验（见 :func:`validate_script_output`），不合格同样重试。

    重试次数可用 ``PAPER2VIDEO_VLM_RETRIES`` 覆盖。
    """
    model = ModelFactory.create(
        model_platform=model_config["model_platform"],
        model_type=model_config["model_type"],
        model_config_dict=model_config.get("model_config"),
        url=model_config.get("url", None),)
    agent = ChatAgent(model=model, system_message="",)

    with open(prompt_path, 'r', encoding='utf-8') as f_prompt: task_prompt = f_prompt.read()
    slide_image_list = [path.join(slide_imgs_dir, name) for name in os.listdir(slide_imgs_dir)]
    slide_image_list = sorted(slide_image_list, key=lambda x: int(re.search(r'\d+', x).group()))

    images = []
    for idx, img_path in enumerate(slide_image_list): images.append(Image.open(img_path))

    # 把「共几张、一张都不能少」写进请求：prompt 模板是静态文件，不知道张数，
    # 而漏页是最典型的输出缺陷，明确告知张数能显著降低它。
    task_prompt = task_prompt.rstrip() + (
        "\n\n【本次输入】共有 {} 张幻灯片。请为**每一张**都生成一段，"
        "一张都不能遗漏，且必须在最后一张之后才结束输出。".format(len(images)))
    messages = BaseMessage.make_user_message(role_name="user", content=task_prompt, image_list=images, meta_dict={})

    attempts = retries if retries is not None else int(
        os.environ.get("PAPER2VIDEO_VLM_RETRIES", "3"))
    last_note = ""
    for attempt in range(1, attempts + 1):
        response = agent.step(messages)
        # 双重防御：response 或 response.msg 都可能是 None
        content = getattr(getattr(response, "msg", None), "content", None)
        if content and content.strip():
            ok, why = validate_script_output(content, len(images))
            if ok:
                return content.strip(), (getattr(response, "info", None) or {}).get("usage", {})
            last_note = "结构不完整：{}".format(why)
            if attempt < attempts:
                wait = 3 * attempt
                print("[subtitle_cursor_gen] 第 {}/{} 次响应不完整（{}；幻灯片 {} 张，"
                      "收到 {} 字符），{}s 后重试".format(
                          attempt, attempts, why, len(images), len(content), wait))
                time.sleep(wait)
            continue
        last_note = repr(response)[:200]
        if attempt < attempts:
            wait = 3 * attempt
            print("[subtitle_cursor_gen] 第 {}/{} 次返回空响应（幻灯片 {} 张，"
                  "prompt {}），{}s 后重试".format(
                      attempt, attempts, len(images), path.basename(prompt_path), wait))
            time.sleep(wait)

    raise RuntimeError(
        "VLM 连续 {} 次未能给出可用的讲稿，放弃。幻灯片 {} 张、prompt={}。\n"
        "  末次问题：{}\n"
        "  排查方向：① 模型/网关侧是否限流或超时；② 输出是否被 max_tokens 截断；"
        "③ 幻灯片过多导致上下文超限；④ 换 --model_name_v 试试。\n"
        "  重试次数可用环境变量 PAPER2VIDEO_VLM_RETRIES 调整。".format(
            attempts, len(images), path.basename(prompt_path), last_note))

    
    

