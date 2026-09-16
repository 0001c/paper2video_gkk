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


def subtitle_cursor_gen(slide_imgs_dir, prompt_path, model_config, retries=None):
    """让 VLM 看全部幻灯片，产出「讲稿 | 光标描述」的逐页文本。

    ⚠️ 为什么必须重试 + 判空
    -----------------------
    实测 VLM **会偶发返回空响应**，camel 只打一行 warning：

        camel.camel.agents.chat_agent - WARNING - No messages returned in
        `step()`. The model returned an empty response.

    此时 ``response.msg`` 是 ``None``。原实现直接 ``response.msg.content``
    会在 ``AttributeError: 'NoneType' object has no attribute 'content'`` 上崩掉
    —— **一次 API 抖动就废掉整条流水线**，而这时前面的幻灯片生成已经跑完
    （可能花了几分钟和若干 token），后面更贵的 TTS + 光标定位全部白费。

    所以这里改成有限次重试（退避），全部失败才抛出**带排查线索**的错误。
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
    messages = BaseMessage.make_user_message(role_name="user", content=task_prompt, image_list=images, meta_dict={})

    attempts = retries if retries is not None else int(
        os.environ.get("PAPER2VIDEO_VLM_RETRIES", "3"))
    last_note = ""
    for attempt in range(1, attempts + 1):
        response = agent.step(messages)
        # 双重防御：response 或 response.msg 都可能是 None
        content = getattr(getattr(response, "msg", None), "content", None)
        if content and content.strip():
            return content.strip(), (getattr(response, "info", None) or {}).get("usage", {})
        last_note = repr(response)[:200]
        if attempt < attempts:
            wait = 3 * attempt
            print("[subtitle_cursor_gen] 第 {}/{} 次返回空响应（幻灯片 {} 张，"
                  "prompt {}），{}s 后重试".format(
                      attempt, attempts, len(images), path.basename(prompt_path), wait))
            time.sleep(wait)

    raise RuntimeError(
        "VLM 连续 {} 次返回空响应，放弃。幻灯片 {} 张、prompt={}。\n"
        "  末次返回：{}\n"
        "  排查方向：① 模型/网关侧是否限流或超时；② 幻灯片过多导致上下文超限；"
        "③ 换 --model_name_v 试试。\n"
        "  重试次数可用环境变量 PAPER2VIDEO_VLM_RETRIES 调整。".format(
            attempts, len(images), path.basename(prompt_path), last_note))
    
    

