'''
    1. (LLM) slide generation 
    2. (VLM) subtitle and cursor prompt generation
    3. TTS->audio; GUI&WhisperX Grounding->cursor;
    4. Talking Gen: local-[hallo2, fantasy, ...], api-[HeyGen]
    5. Merage
'''
import cv2
import pdb
import re
import json
import time
import shutil
import asyncio
import os, sys
import argparse
import subprocess
from os import path

# pv_env 必须最先导入：它负责加载 .env、把缓存指向 E 盘、并把工具目录前置到 PATH
import pv_env
import language_config

# 本仓库自研的 scripts/ 模块（论文配图提取等）。
# ⚠️ 流水线是**子进程**运行的：父进程（web/app.py）往 sys.path 插过 SCRIPTS，
#    但子进程不继承 —— 这里必须自己插一次，否则 import 直接失败。
_SCRIPTS_DIR = path.join(pv_env.REPO_ROOT, "scripts")
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)
try:
    import paper_figures
except Exception:                      # 提图是增强项，import 失败不该拖垮流水线
    paper_figures = None

# 结构化进度（P1）：给 Web 端阶段边界（讲稿生成这类长等待也需要有阶段可显示）。
# 默认不生效；导入失败也不能拖垮流水线。
try:
    import progress_report
except Exception:                      # pragma: no cover
    progress_report = None
from pdf_render import render_pdf_to_images

from speech_gen import tts_per_slide
from subtitle_render import add_subtitles
from talking_gen import talking_gen_per_slide
from merge_talking import merge_slides_and_talking
from merge_video import merge_slides_and_audio
from talking_config import describe_profile, resolve_profile
from cursor_gen import cursor_gen_per_sentence
# from slide_code_gen import latex_code_gen
from slide_code_gen_select_improvement import latex_code_gen_upgrade
from cursor_render import render_video_with_cursor_from_json
import cursor_styles
from subtitle_cursor_prompt_gen import subtitle_cursor_gen, validate_script_output

from wei_utils import get_agent_config


# os.environ["GEMINI_API_KEY"] = ""
# os.environ["OPENAI_API_KEY"] = ""


def read_image_size(image_path):
    """读一张图的尺寸，返回 ``(height, width)``。

    为什么不直接用 ``cv2.imread(...).shape``（原来就是这么写的）——
    实测 cv2 的 imread 会**偶发返回 None**：OpenCV 自己打一行
    ``cv2::findDecoder imread_('<路径>')`` 就放弃，而同一条路径换个读取器
    （PIL / PyMuPDF）能正常打开。原因未完全查明，但这一行位于流水线**最末端**，
    ``.shape`` 一崩就前功尽弃（本次实测：20 分钟的活儿全废在最后一步）。

    所以这里按 cv2 → PIL → PyMuPDF 依次尝试；全部失败才抛错，
    并且把每种读取器的失败原因都写进报错信息，便于下次定位。
    """
    errors = []
    try:
        arr = cv2.imread(image_path)
        if arr is not None:
            return int(arr.shape[0]), int(arr.shape[1])
        errors.append("cv2.imread 返回 None")
    except Exception as exc:
        errors.append("cv2: {}".format(type(exc).__name__))

    try:
        from PIL import Image
        with Image.open(image_path) as img:
            width, height = img.size
        return int(height), int(width)
    except Exception as exc:
        errors.append("PIL: {}".format(type(exc).__name__))

    try:
        import fitz  # PyMuPDF
        with fitz.open(image_path) as doc:
            rect = doc[0].rect
        return int(rect.height), int(rect.width)
    except Exception as exc:
        errors.append("PyMuPDF: {}".format(type(exc).__name__))

    raise RuntimeError(
        "读不出图片尺寸：{}\n  逐种读取器的结果：{}".format(image_path, "；".join(errors)))


def copy_folder(src_dir, dst_dir):
    if not os.path.exists(src_dir): raise FileNotFoundError(f"no such dir: {src_dir}")
    os.makedirs(os.path.dirname(dst_dir), exist_ok=True)
    shutil.copytree(src_dir, dst_dir)

def str2list(s):
    """把 "0,1" 解析成 [0, 1]。

    ⚠️ 空串必须容忍：`--gpu_list` 的默认值就是 ""，而 argparse 会**对字符串默认值
    也套用 type 转换**，于是 `int("")` 会在解析参数阶段直接 ValueError 崩掉 ——
    崩在 main() 之前，报错信息也看不出是哪个参数。空串按「未指定」处理成 []，
    下游 talking_gen/cursor_gen 都有 gpu_list 为空的兜底。
    """
    parts = [p.strip() for p in str(s or "").split(",") if p.strip()]
    return [int(p) for p in parts]


def str2bool(value):
    """argparse 的 type=bool 会把 "False" 也解析成 True（非空字符串恒为真），
    这里显式处理，否则 --if_tree_search False 根本关不掉树搜索。"""
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "y", "on")

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Paper2Video Generation Pipeline')
    parser.add_argument('--result_dir', type=str, default='./result/zeyu')
    parser.add_argument('--model_name_t', type=str, default='deepseek-flash') 
    parser.add_argument('--model_name_v', type=str, default='deepseek-flash') 
    # 数字人后端：hallo2（cu118 无 sm_120）、InfiniteTalk（fp8 需现场编译 quanto
    # CUDA 扩展）、LongCat（注意力序列 36480，sm_120 上无可用高效 kernel）均在本机
    # 不可用，默认换成 MuseTalk —— 它只生成 256x256 人脸（序列仅 1024），标准
    # UNet2DConditionModel + SDPA 即可，结构性绕开该问题。
    parser.add_argument('--model_name_talking', type=str, default='musetalk', choices=['musetalk'])
    parser.add_argument('--talking_profile', type=str, default=None,
                        help='数字人运行档位（见 talking_config.list_profiles()）：'
                             'musetalk-local 本机单卡 / musetalk-server 预留多卡；'
                             'longcat 后端另有 local-16g / server-8xa6000')
    parser.add_argument('--paper_latex_root', type=str, default='./assets/demo/latex_proj')
    parser.add_argument('--ref_img', type=str, default='./assets/demo/zeyu.png')
    # 原始论文文件（.pdf/.docx）。有它就从里面提插图放进工程 —— 上游入口是
    # **带图的** LaTeX 工程，而我们的前端从 PDF/Word 出发只提正文，不提图的话
    # 幻灯片里永远没有 `\includegraphics`（实测：有 2 张图的论文，15 张幻灯片里 0 张图）。
    # 默认 None：老的调用方式（含使用者的 CLI 用法）行为完全不变。
    parser.add_argument('--paper_source', type=str, default=None,
                        help='原始论文文件；给了就从它提取插图到工程里')
    # ⚠️ 默认必须是 None 而不是固定路径：否则下面 `args.ref_audio or lang[...]`
    # 永远选中这里写的路径，语言档位里的参考音频就永远生效不了。
    parser.add_argument('--ref_audio', type=str, default=None,
                        help='参考音频（音色克隆源）；不填则按 --video_lang 自动选')
    parser.add_argument('--ref_text', type=str, default=None,
                        help='参考音频文本；不填则优先读同名 .txt，再退回 ASR 转写')
    parser.add_argument('--tts_model', type=str, default='qwen3tts',
                        choices=['qwen3tts'],
                        help='语音合成后端；qwen3tts 为默认（Apache-2.0）')
    parser.add_argument('--video_lang', type=str, default=None,
                        choices=sorted(language_config.VIDEO_PRESETS),
                        help='输出语言预设：en=全英文（国际会议）；'
                             'zh=英文幻灯片 + 中文讲解（国内组会/中文答辩）')
    parser.add_argument('--paper_type', type=str, default=None,
                        choices=sorted(language_config.PAPER_TYPES),
                        help='论文类型：auto=由模型判断（默认）；'
                             'research=实验型研究；humanities=人文社科。'
                             '显式指定时用确定性指令覆盖 prompt 里的软判据')
    parser.add_argument('--slide_lang', type=str, default=None,
                        choices=sorted(language_config.SLIDE_LANGS),
                        help='只指定幻灯片语言（覆盖 --video_lang 的该轴）')
    parser.add_argument('--script_lang', type=str, default=None,
                        choices=sorted(language_config.SCRIPT_LANGS),
                        help='只指定讲稿/TTS/字幕语言（覆盖 --video_lang 的该轴）')
    parser.add_argument('--script_prompt_file', type=str, default=None,
                        help='自定义讲稿提示词文件（UTF-8）；不填则使用语言档位默认模板')
    parser.add_argument('--slide_prompt_file', type=str, default=None,
                        help='自定义幻灯片提示词文件（UTF-8）；不填则使用语言档位默认模板')
    parser.add_argument('--slide_count', type=int, default=12,
                        help='目标幻灯片页数（含封面与结束页）')
    parser.add_argument('--gpu_list', type=str2list, default="")
    parser.add_argument('--if_tree_search', type=str2bool, default=True)
    parser.add_argument('--reuse_script', type=str2bool, default=False,
                        help='stage 2：若讲稿文件已存在且校验通过，跳过 VLM 调用\n（讲稿生成是唯一必须联网付费的一步，补跑时可省这一次费用）')
    parser.add_argument('--beamer_templete_prompt', type=str, default=None)
    parser.add_argument('--course_plan', type=str, default=None, help='已审核课程教案 JSON')
    parser.add_argument('--script_concurrency', type=int, choices=range(1, 9), default=3, help='讲稿 API 同时请求数（1～8），不影响本地 GPU 串行')
    parser.add_argument('--script_generation_id', type=str, default=None, help='同一次讲稿重写/恢复的缓存标识')
    parser.add_argument('--cursor_style', choices=tuple(cursor_styles.STYLES), default=cursor_styles.DEFAULT)
    parser.add_argument('--cursor_scale', type=float, choices=(0.75, 1, 1.5), default=1)
    parser.add_argument('--review_before_render', type=str2bool, default=False)
    parser.add_argument('--regenerate_pages', type=str, default=None, help='课程局部重写的零基页索引 JSON')
    parser.add_argument('--require_reuse_script', type=str2bool, default=False,
                        help='讲稿不可复用时停止，不自动调用模型重新生成')
    parser.add_argument('--stage', type=str, default="[\"0\"]") 
    parser.add_argument('--talking_head_env', type=str, default="") 
    parser.add_argument('--generate_talking', type=str2bool, default=True,
                        help='是否生成数字人；False 时 stage 3 改为幻灯片+音频直接合片')
    # slide+subtitle: 1; 
    # tts+cusor: 2; 
    # talking-head: 3: 
    # all: 0
    args = parser.parse_args()
    stage = json.loads(args.stage)
    print("start", "stage:", stage, args.gpu_list)

    # 语言档位：把「幻灯片语言 / 讲稿语言」两个轴解析成一份完整配置。
    lang = language_config.resolve(args.slide_lang, args.script_lang,
                                   args.video_lang)
    print("[pipeline] 语言档位：{}".format(lang["desc"]))
    # 论文类型的确定性覆盖指令；auto 时为 None，行为与本层引入之前一致
    slide_extra_prompt = language_config.paper_type_directive(
        args.paper_type, lang["slide_lang"])
    if slide_extra_prompt:
        print("[pipeline] 论文类型显式指定为 {}，已附加覆盖指令".format(
            args.paper_type))
    # ⚠️ ref_text 必须**跟着 ref_audio 走**（2026-09-19 修）。
    # 原先是各自 `or lang[...]`：于是"传了自定义 ref_audio、没传 ref_text"时，
    # 会把**语言档位那一段的文本**配到我们的音频上 —— 文本与音频不是同一句话，
    # 零样本克隆的保真度会明显下降，而且**完全不报错**，只听得出"音色不太像"。
    # 现在：给了自定义音频但没给文本 → 保持 None，交由 speech_gen 走 ASR 转写。
    custom_audio = args.ref_audio
    args.ref_audio = args.ref_audio or lang["ref_audio"]
    args.ref_text = args.ref_text if args.ref_text is not None else (
        None if custom_audio else lang["ref_text"])
    
    cursor_asset, cursor_hotspot = cursor_styles.resolve(args.cursor_style)
    cursor_img_path = str(cursor_asset) if cursor_asset else None
    os.makedirs(args.result_dir, exist_ok=True) # result dir
    import course
    course_plan = course.read_json(args.course_plan) if args.course_plan else None
    if args.course_plan and not course_plan:
        raise ValueError('课程教案不可读取，已停止')
    agent_config_t = get_agent_config(args.model_name_t) # LLM
    agent_config_v = get_agent_config(args.model_name_v) # VLM
    copy_latex_proj_path = path.join(args.result_dir, path.basename(args.paper_latex_root))
    if path.exists(copy_latex_proj_path) is False:
        copy_folder(args.paper_latex_root, copy_latex_proj_path)
    args.paper_latex_root = copy_latex_proj_path
    
    if path.exists(path.join(args.result_dir, "sat.json")) is True:
        with open(path.join(args.result_dir, "sat.json"), 'r', encoding="utf-8") as f:
            time_second = json.load(f)
    else: time_second = {}
        
    if path.exists(path.join(args.result_dir, "token.json")) is True:
        with open(path.join(args.result_dir, "token.json"), 'r', encoding="utf-8") as f:
            token_usage = json.load(f)
    else: token_usage = {}

    # ⚠️ 各阶段的耗时是在各自 stage 块的 if 里写进 time_second 的，没跑的阶段
    # 就不会写。这里先清掉上一轮留下的同名键，避免「本次没跑某阶段，但 sat.json
    # 里还留着上一次的数字」这种误导；收尾时再统一补 None 保证键集合稳定。
    _STAGE_KEYS = ("slide_gen", "subtitle_cursor_prompt_gen", "tts",
                   "cursor_gen", "talking_gen", "merage")
    for _key in _STAGE_KEYS:
        time_second.pop(_key, None)
    
    ## Step 1: Slide Generation
    slide_latex_path = path.join(args.paper_latex_root, "slides.tex")
    slide_image_dir = path.join(args.result_dir, 'slide_imgs')

    # 供光标定位做 PDF 文本坐标匹配。优先用 stage 1 标记过的**同源**副本；
    # 不存在时退回 latex_proj/slides.pdf（template 自带示例的可能是错的，
    # cursor_gen 会用页数校验拦下来并回退 grounding）。
    slide_pdf_path = path.join(args.result_dir, 'slides_rendered.pdf')
    if not path.isfile(slide_pdf_path):
        slide_pdf_path = path.join(args.paper_latex_root, 'slides.pdf')
    os.makedirs(slide_image_dir, exist_ok=True)
    
    start_time = time.time() # start time
    if "1" in stage or  "0" in stage:
        prompt_path = args.slide_prompt_file or lang["slide_prompt"]
        if progress_report:
            progress_report.report("slides", stage=1)
        # ⚠️ 上游这里的 if/else 有一条**永远会崩的分支**：else 里调用的
        # ``latex_code_gen`` 来自 ``slide_code_gen``，而那一行 import 在本文件
        # 第 31 行是**注释掉的**（上游只用 latex_code_gen_upgrade 这一条线）。
        # 于是只要传 ``--if_tree_search False``，就会立刻
        #   NameError: name 'latex_code_gen' is not defined
        # 而 `--if_tree_search` 默认 True，所以这个坑平时不显形 ——
        # 之前几次「跑通了」全是用 pipeline_light.py（它只有单分支）。
        #
        # 修法：与 pipeline_light.py 保持一致，只保留一条分支，把开关**透传**给
        # latex_code_gen_upgrade 自己处理（它在内部决定是否跑「每帧 4 候选 + VLM 选优」）。
        # 行为与参数从此一致，也去掉了对未导入名字的引用。
        # ── 论文配图：提取 + 把清单附进提示词（只在 stage 1 做）──
        #
        # **为什么放在这里**（而不是 web 层）：图的消费方是幻灯片生成，而它读的是
        # `args.paper_latex_root` —— 即**复制后**的 `<result>/latex_proj`。
        # 而 `copy_folder` 只在目标不存在时才执行，所以"重跑/续跑旧任务"时
        # 复制后的目录是旧的；若在 web 层往"源工程"写图，副本里就没有图，
        # 提示词读不到 —— 而日志还会显示"已附清单"（因为它读的是另一份路径）。
        # 放在这里就只有**一个写入点、一个读取点**，物理上不可能不一致。
        #
        # 为什么必须显式给清单：`latex_code_gen_upgrade` 只把工程里的**文件路径**
        # 写进提示词，而 `figures/fig01_p06.jpg` 这种 ASCII 名（文件名刻意不用中文，
        # 理由见 paper_figures 的说明）**毫无语义**，模型无从判断哪张图该放哪一页。
        # 清单把「文件名 → 图题/页码」对上，模型才可能把图用对。
        #
        # ⚠️ 没有图时下面是空串 ⇒ 提示词与"还没接提图"时**逐字节一致**，
        #    所以纯文字论文仍能命中旧的 deck 缓存（指纹里 figures 记为 `none`）。
        _slide_extra = slide_extra_prompt
        _count_directive = (
            "请生成恰好 {0} 页幻灯片（包含封面页与结束页）。在不虚构内容的前提下，"
            "通过合并或拆分主题来满足页数；每个 frame 环境计为一页。".format(args.slide_count)
            if lang.get("slide_lang") == "zh" else
            "Generate exactly {0} slides, including the opening and closing slides. "
            "Meet the count by combining or splitting supported topics without inventing content; "
            "each frame environment counts as one slide.".format(args.slide_count)
        )
        _slide_extra = ((_slide_extra + "\n\n") if _slide_extra else "") + _count_directive
        if course_plan:
            _slide_extra += course.slide_directive(course_plan)
        if paper_figures is not None:
            _figures, _fig_note = paper_figures.extract(args.paper_source,
                                                        args.paper_latex_root)
            if args.paper_source:
                print("[figures] {}".format(_fig_note))
            _fig_block = paper_figures.figure_prompt_block(
                args.paper_latex_root, lang=lang.get("slide_lang", "en"))
            if _fig_block:
                _slide_extra = ((_slide_extra + "\n\n") if _slide_extra else "") + _fig_block
                print("[figures] 已把 {} 张论文配图的清单附给幻灯片生成".format(len(_figures)))
        else:
            print("[figures] ⚠️ paper_figures 未导入（scripts/ 不在 sys.path？）"
                  "—— 本次幻灯片不会有图")

        usage_slide, beamer_path = latex_code_gen_upgrade(
            prompt_path=prompt_path, tex_dir=args.paper_latex_root,
            beamer_save_path=slide_latex_path,
            model_config_ll=agent_config_t, model_config_vl=agent_config_v,
            beamer_temp_name=args.beamer_templete_prompt,
            if_tree_search=args.if_tree_search,
            extra_prompt=_slide_extra)

        # 上游这里用 pdf2image.convert_from_path，但它依赖 poppler（Windows 上要额外装），
        # 而仓库自带的 pdf2img() 本来就是 PyMuPDF 实现。统一改走 PyMuPDF，免装 poppler。
        slide_imgs = render_pdf_to_images(beamer_path, dpi=400)
        for i, img in enumerate(slide_imgs): img.save(path.join(slide_image_dir, f"{i+1}.png")) # save slides as images
        # 另存一份「与 slide_imgs 同源」的 PDF（理由见 pipeline_light 里的说明）
        _rendered_pdf = path.join(args.result_dir, 'slides_rendered.pdf')
        shutil.copy2(beamer_path, _rendered_pdf)
        print("[Stage 1] 已保存同源 PDF -> {}".format(_rendered_pdf))
        if args.model_name_t not in token_usage.keys(): 
            token_usage[args.model_name_t] = [usage_slide]
        else: token_usage[args.model_name_t].append(usage_slide)
        step1_time =  time.time()
        time_second["slide_gen"] = [step1_time-start_time]
        print("Slide Generation", step1_time-start_time)
    
    ## Step 2: Subtitle and Cursor Prompt Generation
    start_time = time.time() # start time
    subtitle_cursor_save_path = path.join(args.result_dir, 'subtitle_w_cursor.txt')
    cursor_save_path = path.join(args.result_dir, 'cursor.json')

    speech_save_dir = path.join(args.result_dir, 'audio')
    if "2" in stage or  "0" in stage:
        prompt_path = args.script_prompt_file or lang["script_prompt"]
        if not path.isfile(prompt_path):
            raise FileNotFoundError("讲稿提示词文件不存在：{}".format(prompt_path))
        # ── --reuse_script：讲稿已存在且校验通过时，跳过 VLM 调用 ──
        # 讲稿生成是整条链路里**唯一必须联网付费**的一步（VLM 要读全部幻灯片）；
        # TTS / 光标定位 / 数字人 / 合片全是本地计算。所以「后续阶段失败、只重跑
        # stage 2」时，没有这个开关就会把已付过费的调用再做一遍。
        reused = False
        if args.reuse_script and path.isfile(subtitle_cursor_save_path):
            _cached = open(subtitle_cursor_save_path, encoding="utf-8").read()
            _n = len([n for n in os.listdir(slide_image_dir)
                      if n.lower().endswith(".png")])
            _ok, _why = validate_script_output(_cached, _n)
            if _ok:
                subtitle, usage_subtitle, reused = _cached, {}, True
                print("[Stage 2] --reuse_script：复用已有讲稿（{} 页，"
                      "跳过 VLM 调用 => 本次无 API 费用）".format(_n))
            else:
                print("[Stage 2] --reuse_script：已有讲稿不合格（{}），改为重新生成"
                      .format(_why))
        if not reused:
            if args.require_reuse_script:
                raise RuntimeError("已有讲稿不可复用，已停止；请在界面明确选择重新生成讲稿后重试")
            # 讲稿生成要读全部幻灯片图，是整条链路里最慢的一次 API 调用（约 1~2 分钟）。
            # 先报一条阶段进度，界面才不会在这一段里"看着像没开始"。
            if progress_report:
                progress_report.report("script", stage=2)
            if course_plan:
                subtitle, usage_subtitle = course.generate_script(course_plan, slide_image_dir,
                    prompt_path, agent_config_v, args.result_dir, subtitle_cursor_gen,
                    selected=json.loads(args.regenerate_pages) if args.regenerate_pages else None, workers=args.script_concurrency,
                    generation_id=args.script_generation_id)
            elif args.regenerate_pages:
                subtitle, usage_subtitle = course.generate_selected(slide_image_dir, prompt_path,
                    agent_config_v, args.result_dir, subtitle_cursor_gen, json.loads(args.regenerate_pages), workers=args.script_concurrency,
                    generation_id=args.script_generation_id)
            else:
                subtitle, usage_subtitle = course.generate_script(None,
                    slide_image_dir, prompt_path, agent_config_v, args.result_dir, subtitle_cursor_gen, workers=args.script_concurrency,
                    generation_id=args.script_generation_id)
            # 显式 utf-8：读回处固定用 utf-8，写入端不该依赖环境默认编码
            course.save_script(subtitle_cursor_save_path, subtitle)
        if args.review_before_render:
            course.write_json(path.join(args.result_dir, 'course_review.json'), {'ready': True})
            if course_plan:
                course.write_json(path.join(args.result_dir, 'course_timing.json'), course.audio_report(course_plan, args.result_dir))
            print('[course] 课件与讲稿已准备，请审核后再合成音频与视频', flush=True)
            sys.exit(0)
        if args.model_name_v not in token_usage.keys(): 
            token_usage[args.model_name_v] = [usage_subtitle]
        else: token_usage[args.model_name_v].append(usage_subtitle)
        step2_time =  time.time()
        time_second["subtitle_cursor_prompt_gen"] = [step2_time-start_time]
        print("Subtitle and Cursor Prompt Generation", step2_time-start_time)

        ## Step 3-1: Speech Generation
        # ⚠️ 原来写死 model_type='f5'，而 F5-TTS 已因许可证（CC-BY-NC）从默认
        # 依赖里移除，speech_gen.inference_f5 会直接抛 RuntimeError
        # -> 主流水线的 stage 2 曾经必然失败。改为可配置，默认 qwen3tts。
        tts_per_slide(model_type=args.tts_model, script_path=subtitle_cursor_save_path,
                    speech_save_dir=speech_save_dir, ref_audio=args.ref_audio,
                    ref_text=args.ref_text, language=lang["tts_language"], resume_audio=True)
        step3_1_time =  time.time()
        time_second["tts"] = [step3_1_time-step2_time]
        print("Speech Generation", step3_1_time-step2_time)
        
        ## Step 3-2: Cursor Generation
        os.environ["PYTHONHASHSEED"] = "random"        
        if progress_report:
            progress_report.report("cursor", stage=2)
        cursor_token = cursor_gen_per_sentence(script_path=subtitle_cursor_save_path, slide_img_dir=slide_image_dir, 
                                slide_audio_dir=speech_save_dir, cursor_save_path=cursor_save_path, gpu_list=args.gpu_list,
                                language=lang["align_language"], slide_pdf_path=slide_pdf_path,
                                resume_cache=True)
        if course_plan:
            course.export_chapters(course_plan, args.result_dir)
        token_usage["cursor"] = cursor_token
        step3_2_time =  time.time()
        time_second["cursor_gen"] = [step3_2_time-step3_1_time]
        print("Cursor Generation", step3_2_time-step3_1_time)
    
    ## Step 4: Talking Video Generation（数字人，可关闭）
    start_time = time.time() # start time
    if "3" in stage or  "0" in stage:
        tmp_merage_dir = path.join(args.result_dir, "merage")
        tmp_merage_1 = path.join(args.result_dir, "1_merage.mp4")
        image_height, image_width = read_image_size(path.join(slide_image_dir, '1.png'))
        size = max(image_height // 6, image_width // 6)
        num_slide = len([n for n in os.listdir(slide_image_dir) if n.lower().endswith(".png")])

        if args.generate_talking:
            if not args.ref_img or not path.isfile(args.ref_img):
                raise FileNotFoundError("已要求生成数字人，但人像图不存在：{}".format(
                    args.ref_img or "<未提供>"))
            print("[数字人] 档位：{}".format(describe_profile(
                resolve_profile(args.talking_profile, backend=args.model_name_talking))))
            talking_save_dir = path.join(
                args.result_dir, 'talking_{}'.format(args.model_name_talking))
            talking_inference_input = []
            # ⚠️ 必须按**数字**排序：文件名是 0.wav..11.wav，而 os.listdir 顺序不保证，
            # 且字典序会把 10.wav / 11.wav 排到 2.wav 前面。一旦顺序错位，数字人就会
            # 对错音频，而症状是「口型与语音不符」——很难联想到是排序问题。
            audio_names = [n for n in os.listdir(speech_save_dir)
                           if n.lower().endswith(".wav")]
            audio_names = sorted(
                audio_names, key=lambda x: int(re.search(r"\d+", x).group()))
            for name in audio_names:
                talking_inference_input.append(
                    [args.ref_img, path.join(speech_save_dir, name)])
            talking_gen_per_slide(
                args.model_name_talking, talking_inference_input, talking_save_dir,
                args.gpu_list, env_path=args.talking_head_env,
                profile=args.talking_profile)
            step4_time = time.time()
            time_second["talking_gen"] = [step4_time-start_time]
            print("Talking Generation", step4_time-start_time)

            # 数字人嵌在右上角，边长取画面长边的 1/6（沿用上游的版式）
            ref_stem = path.splitext(path.basename(args.ref_img))[0]
            merge_slides_and_talking(
                slide_dir=slide_image_dir, talking_dir=talking_save_dir,
                output_dir=tmp_merage_dir, video_path=tmp_merage_1,
                num_video=num_slide, ref_img=ref_stem, width=size, height=size)
        else:
            # 没有人像时不能跳过整个 stage 3：最终合片、光标与字幕都在这里。
            # 只跳过数字人推理，改为把每页幻灯片按对应音频时长直接合成。
            print("[数字人] 已关闭或未提供人像，跳过数字人生成，直接合成讲解视频")
            time_second["talking_gen"] = None
            step4_time = start_time
            merge_slides_and_audio(
                slide_dir=slide_image_dir, audio_dir=speech_save_dir,
                output_dir=tmp_merage_dir, num_video=num_slide,
                video_path=tmp_merage_1)

        ## Step5: Merage
        # render cursor
        cursor_size = max(1, round(size / 6 * args.cursor_scale))
        if progress_report:
            progress_report.report("merge", stage=3)
        tmp_merage_2 = path.join(args.result_dir, "2_merage.mp4")
        render_video_with_cursor_from_json(video_path=tmp_merage_1, out_video_path=tmp_merage_2, 
                                        json_path=cursor_save_path, cursor_img_path=cursor_img_path, 
                                        transition_duration=0.1, cursor_size=cursor_size, cursor_hotspot=cursor_hotspot)
        # render subtitle
        front_size = size//10
        tmp_merage_3 = path.join(args.result_dir, "3_merage.mp4")
        add_subtitles(tmp_merage_2, tmp_merage_3, size//10,
                      font_name=lang["subtitle_font"])
        if course_plan:
            course.embed_chapters(course_plan, args.result_dir)
        step5_time =  time.time()
        time_second["merage"] = [step5_time-step4_time]
        print("Merage", step5_time-step4_time)
        
    # sat. save
    # ⚠️ 这里刻意**不**重建 time_second。原写法是
    #     time_second = {"slide_gen": [step1_time-start_time, usage_slide], ...}
    # 有两个问题：
    #   ① 引用了只在 stage 1/2 的 if 里赋值的 step1_time / step2_time /
    #      step3_1_time / step3_2_time / usage_slide / usage_subtitle ——
    #      于是「只跑 stage 3（数字人+合成），复用已有的 slide/audio」这个很常用
    #      的用法会直接 NameError 崩在最后一步（sat.json 保持旧内容，极具迷惑性）；
    #   ② 会把各 stage 块里刚写好的真实耗时整段覆盖掉（那些是本轮实测值）。
    # 改为：各阶段块自己写自己那段，这里只补齐「没跑的阶段」为 None。
    for _key in _STAGE_KEYS:
        time_second.setdefault(_key, None)
    # ⚠️ JSON 的读写都显式 utf-8：这几份文件现在恰好是纯 ASCII（json.dump 默认
    # ensure_ascii=True 会把非 ASCII 转义），所以平台编码不影响结果 —— 但那是**运气**，
    # 一旦哪天往 token_usage 里放了中文（例如模型名、备注），GBK 默认就会养成
    # "写入 GBK、读取按 UTF-8"的错配。cost：一行参数。
    with open(path.join(args.result_dir, "sat.json"), 'w', encoding="utf-8") as f: json.dump(time_second, f, indent=4)
    with open(path.join(args.result_dir, "token.json"), 'w', encoding="utf-8") as f: json.dump(token_usage, f, indent=4)
