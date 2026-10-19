import re
import time
import json
import uuid
import pdb
import os, sys
from os import path
from PIL import Image
from pathlib import Path
from camel.messages import BaseMessage
from camel.types import ModelPlatformType
from wei_utils import create_agent
from script_validation import validate_script_output, normalize_script
from classroom_language import CLASSROOM_PROMPT


def _save_rejected_response(directory, image_paths, attempt, content, why, response):
    if directory is None:
        return
    usage = (getattr(response, 'info', None) or {}).get('usage') or {}
    record = {'images': [Path(name).name for name in image_paths], 'attempt': attempt,
              'validation_error': why, 'content': content,
              'usage': {key: usage[key] for key in ('prompt_tokens', 'completion_tokens', 'total_tokens')
                        if isinstance(usage.get(key), int)}}
    try:
        root = Path(directory)
        root.mkdir(parents=True, exist_ok=True)
        target = root / ('rejected-' + uuid.uuid4().hex + '.json')
        target.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding='utf-8')
        print('[subtitle_cursor_gen] 校验失败原始响应已保存：{}'.format(target), flush=True)
    except OSError as exc:
        print('[subtitle_cursor_gen] 无法保存格式诊断（{}）'.format(type(exc).__name__), flush=True)


def _empty_response_hint(response, model_config):
    """把「空响应」**当场**区分成「被截断」还是「真·空」，别等人工回头猜。

    为什么需要这个函数
    ------------------
    这两种根因在日志上**长得一模一样**（camel 都只给 ``msgs=[]``），
    但修法完全相反：

    - **输出被 max_tokens 截断** → 调大上限或分批输出。
    - **真的没产出**（欠费 / 限流 / 鉴权）→ 先查余额与额度。

    本项目**已经为此误判过两次**：第一次判成「余额耗尽」、第二次判成「网关抖动」，
    两次排查方向全错。而第二次的真相就摆在 usage 里
    （``completion_tokens=16386`` 贴死上限 16384），只是当时没打印出来。

    判据（只有一条，优先于一切猜测）：
        completion_tokens 是否 **贴着** max_tokens。贴着 = 截断。
    注意推理模型的**思维链也计入 completion_tokens**，所以"正文很短却撞顶"
    是正常的 —— 额度是被思考吃掉的。
    """
    usage = (getattr(response, "info", None) or {}).get("usage") or {}
    completion = usage.get("completion_tokens")
    limit = (model_config.get("model_config") or {}).get("max_tokens") \
        if isinstance(model_config, dict) else None

    if completion is None:
        return ("usage 里没有 completion_tokens → 模型侧未返回内容，"
                "先查 API 余额 / 限流 / 鉴权")
    if limit and completion >= limit * 0.98:
        return ("completion_tokens={} 已贴着上限 max_tokens={} → **输出被截断**"
                "（推理模型的思维链也计入 completion_tokens）。"
                "修法：调大 PAPER2VIDEO_MAX_TOKENS 或分批输出 —— 不要怀疑网络"
                .format(completion, limit))
    if not completion:
        return "completion_tokens=0 → 模型没产出任何 token，先查余额 / 限流 / 鉴权"
    return ("completion_tokens={}（未贴上限却解析不出消息）→ 多半是输出格式"
            "不符合预期，检查 prompt 与该模型的能力匹配".format(completion))


def subtitle_cursor_gen(slide_imgs_dir, prompt_path, model_config, retries=None,
                        image_paths=None, prompt_text=None, diagnostics_dir=None):
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
    agent = create_agent(model_config)

    with open(prompt_path, 'r', encoding='utf-8') as f_prompt: task_prompt = prompt_text or f_prompt.read()
    slide_image_list = image_paths or [path.join(slide_imgs_dir, name) for name in os.listdir(slide_imgs_dir) if name.lower().endswith('.png')]
    slide_image_list = sorted(slide_image_list, key=lambda x: int(re.search(r'\d+', x).group()))

    images = []
    for img_path in slide_image_list:
        with Image.open(img_path) as img:
            images.append(img.copy())  # 立即释放文件句柄；失败与并发请求也不能锁住课件文件。

    # 把「共几张、一张都不能少」写进请求：prompt 模板是静态文件，不知道张数，
    # 而漏页是最典型的输出缺陷，明确告知张数能显著降低它。
    task_prompt = task_prompt.rstrip() + (
        "\n\n【本次输入】共有 {} 张幻灯片。请为**每一张**都生成一段，"
        "一张都不能遗漏，且必须在最后一张之后才结束输出。".format(len(images)))
    task_prompt += ('\n【机器可读输出；覆盖上方旧的逐行讲稿格式和示例】只返回 JSON：{"pages":[{"sentences":[{"speech_text":"口播",'
                    '"cursor_prompt":"课件原文短语或 no"}]}]}。不要分行写口播与目标，不要伪脚注。'
                    'speech_text 只放口播内容，不混入旧的“讲稿 | 光标目标”行或 ### 页分隔符；每句分别放在 sentences 中。'
                    'cursor_prompt 只放原文短语，多个目标不要用 | 拼接；无需光标时写字符串 "no"，不能写裸 no。'
                    '保留年份、小数、百分比，不要凭常识补写课件未提供的事实、公式或演示承诺。')
    task_prompt += CLASSROOM_PROMPT
    messages = BaseMessage.make_user_message(role_name="user", content=task_prompt, image_list=images, meta_dict={})

    attempts = retries if retries is not None else int(
        os.environ.get("PAPER2VIDEO_VLM_RETRIES", "3"))
    last_note = ""
    format_failed = False
    for attempt in range(1, attempts + 1):
        try:
            response = agent.step(messages)
        except Exception as exc:
            # 并发可能触发服务商限流；仅重试瞬时错误，鉴权／参数错误立即报告。
            from openai import APIConnectionError, APITimeoutError
            transient = getattr(exc, 'status_code', None) in (429, 500, 502, 503, 504) or isinstance(exc, (APIConnectionError, APITimeoutError))
            if not transient or attempt >= attempts:
                raise
            print('[subtitle_cursor_gen] 请求暂时失败，第 {}/{} 次，{}s 后重试'.format(attempt, attempts, 3 * attempt), flush=True)
            time.sleep(3 * attempt)
            continue
        # 双重防御：response 或 response.msg 都可能是 None
        content = getattr(getattr(response, "msg", None), "content", None)
        if content and content.strip():
            ok, why = validate_script_output(content, len(images))
            if ok:
                return normalize_script(content), (getattr(response, "info", None) or {}).get("usage", {})
            format_failed = True
            last_note = "讲稿格式校验失败：{}".format(why)
            _save_rejected_response(diagnostics_dir, slide_image_list, attempt, content, why, response)
            if attempt < attempts:
                wait = 3 * attempt
                print("[subtitle_cursor_gen] 第 {}/{} 次响应不完整（{}；幻灯片 {} 张，"
                      "收到 {} 字符），{}s 后重试".format(
                          attempt, attempts, why, len(images), len(content), wait))
                agent.reset()
                messages = BaseMessage.make_user_message(role_name='user', content=task_prompt +
                    '\n【上次格式校验失败】' + why + '。请重新输出完整、合法的JSON；不得遗漏当前页内容或用空字符串代替。',
                    image_list=images, meta_dict={})
                time.sleep(wait)
            continue
        format_failed = False
        usage = (getattr(response, "info", None) or {}).get("usage") or {}
        hint = _empty_response_hint(response, model_config)
        last_note = "{} | usage={}".format(repr(response)[:300], usage)
        if attempt < attempts:
            wait = 3 * attempt
            print("[subtitle_cursor_gen] 第 {}/{} 次返回空响应（幻灯片 {} 张，"
                  "prompt {}）\n    判定：{}\n    {}s 后重试".format(
                      attempt, attempts, len(images), path.basename(prompt_path),
                      hint, wait))
            time.sleep(wait)

    if format_failed:
        raise RuntimeError('VLM 连续 {} 次未能给出可用的讲稿，幻灯片 {} 张、prompt={}。\n'
                           '  末次问题：{}\n'
                           '  接口已返回内容，失败发生在讲稿格式校验；已完成页的缓存保留，重试只补未完成页。'
                           .format(attempts, len(images), path.basename(prompt_path), last_note))
    raise RuntimeError(
        "VLM 连续 {} 次未能给出可用的讲稿，放弃。幻灯片 {} 张、prompt={}。\n"
        "  末次问题：{}\n"
        "  排查顺序（按命中率排，不要跳步）：\n"
        "    ① 看上面 usage 的 completion_tokens 是否贴着 max_tokens ——"
        "贴着就是**输出被截断**（推理模型思维链也占额度），"
        "调大 PAPER2VIDEO_MAX_TOKENS 或分批输出，**不要先怀疑网络**；\n"
        "    ② 再查 API 余额与限流（欠费时网关返回空、不给错误码，症状与①一样）；\n"
        "    ③ 幻灯片过多导致上下文超限；\n"
        "    ④ 换 --model_name_v 试试。\n"
        "  重试次数可用环境变量 PAPER2VIDEO_VLM_RETRIES 调整。".format(
            attempts, len(images), path.basename(prompt_path), last_note))

    
    

