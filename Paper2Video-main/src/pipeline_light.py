"""无数字人的生成入口。

兼容 API 生成课件与讲稿，本地 Qwen3 语音/对齐和 GUI-Owl 定位后合片。
"""

import os
import sys
import json
import time
import argparse
import shutil
import subprocess

# 必须在其它模块之前导入：pv_env 负责加载 .env、指向 E 盘缓存、前置工具目录 PATH
import pv_env

from merge_video import merge_slides_and_audio
from pdf_render import render_pdf_to_images
from speech_gen import tts_per_slide, release_models
from subtitle_render import add_subtitles
from cursor_gen import cursor_gen_per_sentence, release_gui_model
from slide_code_gen_select_improvement import latex_code_gen_upgrade
from cursor_render import render_video_with_cursor_from_json
import cursor_styles
from subtitle_cursor_prompt_gen import subtitle_cursor_gen

import language_config

from wei_utils import get_agent_config
from model_runtime import apply_model_snapshot


def copy_folder(src_dir, dst_dir):
    import shutil
    if not os.path.exists(src_dir):
        raise FileNotFoundError("no such dir: {}".format(src_dir))
    os.makedirs(os.path.dirname(dst_dir), exist_ok=True)
    shutil.copytree(src_dir, dst_dir)


def str2bool(value):
    """argparse 的 type=bool 会把 "False" 也解析成 True，这里显式处理。"""
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "y", "on")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Paper2Video Generation Pipeline (light)")
    parser.add_argument("--result_dir", type=str, default="./result/zeyu")
    # API 侧统一收敛到 DeepSeek（OpenAI 兼容接口，模型名走 wei_utils 的兜底分支）
    parser.add_argument("--model_name_t", type=str, default="deepseek-flash")
    parser.add_argument("--model_name_v", type=str, default="deepseek-flash")
    parser.add_argument("--paper_latex_root", type=str, default="./assets/demo/latex_proj")
    parser.add_argument("--ref_img", type=str, default="./assets/demo/zeyu.png")
    # ⚠️ 默认必须是 None 而不是某个固定路径：否则下面的
    # `args.ref_audio or lang["ref_audio"]` 永远选中这里写的路径，
    # 语言档位里的参考音频就永远生效不了（中文模式会错用英文参考音频）。
    parser.add_argument("--ref_audio", type=str, default=None,
                        help="参考音频（音色克隆源）；不填则按 --video_lang 自动选："
                             "en -> assets/demo/zeyu.wav，zh -> assets/demo/zeyu_zh.wav")
    parser.add_argument("--ref_text", type=str, default=None,
                        help="参考音频的文本；不填则优先读同名 .txt，再退回 Qwen3-ASR 转写")
    parser.add_argument("--gpu_list", type=str, default="0",
                        help="逗号分隔的 GPU 序号；单卡填 0。留空会被兜底为 0")
    parser.add_argument("--paper_type", type=str, default=None,
                        choices=sorted(language_config.PAPER_TYPES),
                        help="论文类型：auto=由模型判断（默认）；"
                             "research=实验型研究；humanities=人文社科")
    parser.add_argument("--if_tree_search", type=str2bool, default=True)
    parser.add_argument("--beamer_templete_prompt", type=str, default=None)
    parser.add_argument("--stage", type=str, default='["0"]')
    parser.add_argument('--model-config', dest='model_config', default=None, help='无秘密的任务模型配置快照路径')
    parser.add_argument("--tts_model", type=str, default="qwen3tts",
                        choices=["qwen3tts"],
                        help="语音合成后端；qwen3tts 为默认（Apache-2.0）")
    parser.add_argument("--tts_language", type=str, default=None,
                        help="TTS/对齐语种；留空则跟随语言档位（见 --video_lang）")
    parser.add_argument("--video_lang", type=str, default=None,
                        choices=sorted(language_config.VIDEO_PRESETS),
                        help="输出语言预设，一次性定下「幻灯片语言 + 讲稿语言」。"
                             "en=全英文（国际会议）；"
                             "zh=英文幻灯片 + 中文讲解（国内组会/中文答辩）。"
                             "细节见 language_config.py")
    parser.add_argument("--slide_lang", type=str, default=None,
                        choices=sorted(language_config.SLIDE_LANGS),
                        help="只指定幻灯片语言（覆盖 --video_lang 的这一轴）")
    parser.add_argument("--script_lang", type=str, default=None,
                        choices=sorted(language_config.SCRIPT_LANGS),
                        help="只指定讲稿/TTS/字幕语言（覆盖 --video_lang 的这一轴）")
    parser.add_argument("--talking_head_env", type=str, default="")
    parser.add_argument("--skip_slide_gen", type=str2bool, default=False,
                        help="跳过 LLM 生成幻灯片，直接编译现有 slides.tex（调参时省 token）")
    parser.add_argument('--cursor_style', choices=tuple(cursor_styles.STYLES), default=cursor_styles.DEFAULT)
    parser.add_argument('--cursor_scale', type=float, choices=(0.75, 1, 1.5), default=1)
    args = parser.parse_args()
    model_snapshot = apply_model_snapshot(args)

    stage = json.loads(args.stage)
    print("start", "stage:", stage, "gpu_list:", args.gpu_list)
    pv_env.print_banner()

    # 语言档位：把「幻灯片语言 / 讲稿语言」两个轴解析成一份完整配置。
    # 没显式给 --ref_audio / --ref_text 时跟随语言档位 —— 中文讲稿要用中文参考
    # 音频（不同语言共用一段参考音频会让音色和发音都别扭）。
    lang = language_config.resolve(args.slide_lang, args.script_lang, args.video_lang)
    print("[pipeline] 语言档位：{}".format(lang["desc"]))
    # ⚠️ ref_text 必须**跟着 ref_audio 走**（2026-09-19 修，理由同 pipeline.py）：
    # 各自 `or lang[...]` 会把语言档位的文本配到自定义音频上，克隆保真度下降
    # 且不报错。给了自定义音频却没给文本时保持 None，交给 ASR 转写。
    custom_audio = args.ref_audio
    args.ref_audio = args.ref_audio or lang["ref_audio"]
    args.ref_text = args.ref_text if args.ref_text is not None else (
        None if custom_audio else lang["ref_text"])
    if not args.tts_language:
        args.tts_language = lang["tts_language"]

    cursor_asset, cursor_hotspot = cursor_styles.resolve(args.cursor_style)
    cursor_img_path = str(cursor_asset) if cursor_asset else None
    os.makedirs(args.result_dir, exist_ok=True)
    os.environ['PAPER2VIDEO_USAGE_DIR'] = os.path.abspath(args.result_dir)
    active_roles = args.required_model_roles if model_snapshot and model_snapshot['model_config_origin'] == 'v2' else ('text', 'vision')
    agent_config_t = get_agent_config(args.model_name_t, 'text', model_snapshot) if 'text' in active_roles else None
    agent_config_v = get_agent_config(args.model_name_v, 'vision', model_snapshot) if 'vision' in active_roles else None

    copy_latex_proj_path = os.path.join(args.result_dir, os.path.basename(args.paper_latex_root))
    if os.path.exists(copy_latex_proj_path) is False:
        copy_folder(args.paper_latex_root, copy_latex_proj_path)
    args.paper_latex_root = copy_latex_proj_path

    sat_path = os.path.join(args.result_dir, "sat.json")
    time_second = json.load(open(sat_path, encoding="utf-8")) if os.path.exists(sat_path) else {}

    token_path = os.path.join(args.result_dir, "token.json")
    token_usage = json.load(open(token_path, encoding="utf-8")) if os.path.exists(token_path) else {}

    slide_image_dir = os.path.join(args.result_dir, "slide_imgs")

    # 供光标定位做 PDF 文本坐标匹配。优先用 stage 1 标记过的**同源**副本；
    # 不存在时退回 latex_proj/slides.pdf —— 但那份可能是模板自带的示例，
    # 所以 cursor_gen 会校验「页数 == 幻灯片张数」，不符就放弃 PDF 定位
    # 并回退 grounding（宁可退化，也不要错位）。
    slide_pdf_path = os.path.join(args.result_dir, "slides_rendered.pdf")
    if not os.path.isfile(slide_pdf_path):
        slide_pdf_path = os.path.join(args.paper_latex_root, "slides.pdf")
    speech_save_dir = os.path.join(args.result_dir, "audio")
    subtitle_cursor_save_path = os.path.join(args.result_dir, "subtitle_w_cursor.txt")
    cursor_save_path = os.path.join(args.result_dir, "cursor.json")
    teaching = None
    if args.paper_type == 'textbook':
        import course
        from teaching_quality import source_examples
        teaching = {'examples':source_examples(course.latex_source(args.paper_latex_root)),
                    'speech_rate':220 if lang['script_lang'] == 'zh' else 140,'script_lang':lang['script_lang']}
        course.write_json(os.path.join(args.result_dir,'teaching_contract.json'),teaching)

    # ── Step 1: 幻灯片生成 ──
    slide_latex_path = os.path.join(args.paper_latex_root, "slides.tex")
    os.makedirs(slide_image_dir, exist_ok=True)

    if "1" in stage or "0" in stage:
        start_time = time.time()

        if args.skip_slide_gen:
            print("[Stage 1] 跳过 LLM 生成，直接使用现有 slides.tex: {}".format(slide_latex_path))
            from slide_code_gen_select_improvement import compile_tex
            ok, feedback = compile_tex(slide_latex_path)
            if not ok:
                raise ValueError('课件未通过质量门禁：'+feedback[-2000:])
            beamer_path = slide_latex_path.replace(".tex", ".pdf")
            usage_slide = {}
        else:
            # 幻灯片 prompt 由语言档位决定（中文幻灯片那一路尚未实现，
            # 见 language_config.SLIDE_LANGS 的注释）
            prompt_path = lang["slide_prompt"]
            slide_extra_prompt = language_config.paper_type_directive(
                args.paper_type, lang["slide_lang"])
            if teaching:
                from teaching_quality import directive
                slide_extra_prompt = (slide_extra_prompt or '')+directive(teaching['examples'])
            if slide_extra_prompt:
                print("[pipeline] 论文类型显式指定为 {}，已附加覆盖指令".format(
                    args.paper_type))
            usage_slide, beamer_path = latex_code_gen_upgrade(
                prompt_path=prompt_path, tex_dir=args.paper_latex_root,
                beamer_save_path=slide_latex_path,
                model_config_ll=agent_config_t, model_config_vl=agent_config_v,
                beamer_temp_name=args.beamer_templete_prompt,
                # 必须显式转发：latex_code_gen_upgrade 里 if_tree_search 默认 True，
                # 不传的话 --if_tree_search 这个 CLI 开关就是死的 —— 用户明明传了
                # False，代码仍会去跑「每帧 4 个候选布局 + VLM 选优」，既费 token
                # 又费时间（实测多花 ~3 分钟、7 次 VLM 调用），且行为与参数不符。
                if_tree_search=args.if_tree_search,
                extra_prompt=slide_extra_prompt)

        # latex_code_gen_upgrade 的第二个返回值是 **PDF 路径**（它内部只负责编译，
        # 不负责出图），因此两个分支统一在这里把 PDF 逐页渲染成 PNG。
        # 上游只有 skip 分支出图，正常跑 LLM 时 slide_imgs/ 会一直是空的。
        if not os.path.exists(beamer_path):
            raise FileNotFoundError(
                "编译后未生成 PDF：{}。请确认 tectonic 已安装"
                "（./.venv/Scripts/python.exe scripts/setup_tools.py）".format(beamer_path))
        images = render_pdf_to_images(beamer_path, dpi=400)
        for index, image in enumerate(images):
            image.save(os.path.join(slide_image_dir, "{}.png".format(index + 1)))
        print("[Stage 1] 渲染 {} 张幻灯片 -> {}".format(len(images), slide_image_dir))

        # 把**渲染所依据的 PDF** 另存一份，明确标记它与 slide_imgs 同源。
        # 为什么需要：光标定位会拿 PDF 做文本坐标匹配，而 latex_proj/slides.pdf
        # 未必是本次编译的产物 —— pipeline 会把 --paper_latex_root **整个目录**
        # 复制到 result 下，模板里自带的示例 slides.pdf 会被一起带过来
        # （实测：模板示例 16 页 vs 本次 12 页，页数都对不上）。错位的坐标比
        # 没有坐标更糟，所以这里留一份可信的。
        rendered_pdf = os.path.join(args.result_dir, "slides_rendered.pdf")
        shutil.copy2(beamer_path, rendered_pdf)
        print("[Stage 1] 已保存同源 PDF -> {}".format(rendered_pdf))

        if args.model_name_t not in token_usage:
            token_usage[args.model_name_t] = [usage_slide]
        else:
            token_usage[args.model_name_t].append(usage_slide)

        elapsed = time.time()
        time_second["slide_gen"] = [elapsed - start_time]
        print("[Stage 1] 幻灯片生成耗时 {:.1f}s".format(elapsed - start_time))

    # ── Step 2: 字幕 + 光标提示 / TTS / 光标定位 ──
    if "2" in stage or "0" in stage:
        start_time = time.time()
        from slide_quality import require_quality
        require_quality(args.paper_latex_root)

        prompt_path = lang["script_prompt"]
        if teaching:
            subtitle, usage_subtitle = course.generate_script(None,slide_image_dir,prompt_path,agent_config_v,
                args.result_dir,subtitle_cursor_gen,teaching=teaching)
            report = course.teaching_review(course.split_pages(subtitle),args.result_dir,teaching)
            course.write_json(os.path.join(args.result_dir,'teaching_report.json'),report)
            if not report['passed']:
                raise ValueError('教学内容未通过收费前审核：'+'；'.join(report['issues']))
        else:
            subtitle, usage_subtitle = subtitle_cursor_gen(slide_image_dir, prompt_path, agent_config_v)
        with open(subtitle_cursor_save_path, "w", encoding="utf-8") as handle:
            handle.write(subtitle)
        if args.model_name_v not in token_usage:
            token_usage[args.model_name_v] = [usage_subtitle]
        else:
            token_usage[args.model_name_v].append(usage_subtitle)

        after_subtitle = time.time()
        time_second["subtitle_cursor_prompt_gen"] = [after_subtitle - start_time]
        print("[Stage 2-1] 字幕+光标提示生成 {:.1f}s".format(after_subtitle - start_time))

        # -- 语音合成 --
        tts_per_slide(
            model_type=args.tts_model,
            script_path=subtitle_cursor_save_path,
            speech_save_dir=speech_save_dir,
            ref_audio=args.ref_audio,
            ref_text=args.ref_text,
            language=args.tts_language,
            resume_audio=True,
            model_snapshot=model_snapshot,
        )
        after_tts = time.time()
        time_second["tts"] = [after_tts - after_subtitle]
        print("[Stage 2-2] 语音合成 {:.1f}s".format(after_tts - after_subtitle))

        # TTS 权重约 4 GB，用完立刻释放，把显存让给后面 9 GB 的 GUI-Owl。
        # 16 GB 卡上这一步是「能跑」与「OOM」的分界。
        release_models(("tts",))

        # -- 光标定位 + 时间轴 --
        os.environ["PYTHONHASHSEED"] = "random"
        cursor_token = cursor_gen_per_sentence(
            script_path=subtitle_cursor_save_path,
            slide_img_dir=slide_image_dir,
            slide_audio_dir=speech_save_dir,
            cursor_save_path=cursor_save_path,
            gpu_list=args.gpu_list,
            # 用档位里的 align_language 而不是 tts_language：这两者虽然
            # 目前取值相同，但语义不同（一个是合成语种、一个是强制对齐的语种），
            # 分开后将来「中文讲稿配英文参考音频」之类才不会串。
            language=lang["align_language"],
            slide_pdf_path=slide_pdf_path,
            resume_cache=True,
            model_snapshot=model_snapshot,
            timing_source=model_snapshot['model_config']['timing']['source'] if model_snapshot else 'local_forced_aligner',
        )
        token_usage["cursor"] = cursor_token
        after_cursor = time.time()
        time_second["cursor_gen"] = [after_cursor - after_tts]
        print("[Stage 2-3] 光标定位 {:.1f}s".format(after_cursor - after_tts))

        # grounding 已完成，释放 GUI-Owl 给合片阶段腾显存
        release_gui_model()

    # ── Step 3: 合片（本版不含数字人） ──
    if "3" in stage or "0" in stage:
        import cv2

        start_time = time.time()
        tmp_merage_dir = os.path.join(args.result_dir, "merage")
        tmp_merage_1 = os.path.join(args.result_dir, "1_merage.mp4")

        sample = os.path.join(slide_image_dir, "1.png")
        if not os.path.exists(sample):
            raise FileNotFoundError("找不到幻灯片图片：{}（请先跑 stage 1）".format(sample))
        image_shape = cv2.imread(sample).shape
        size = max(image_shape[0] // 6, image_shape[1] // 6)
        num_slide = len([n for n in os.listdir(slide_image_dir) if n.lower().endswith(".png")])

        # 原实现调用 ./1_merage_light.bash（Windows 不可执行），这里改走 Python
        merge_slides_and_audio(
            slide_dir=slide_image_dir,
            audio_dir=speech_save_dir,
            output_dir=tmp_merage_dir,
            num_video=num_slide,
            video_path=tmp_merage_1,
            ref_img=os.path.basename(args.ref_img).replace(".png", ""),
        )

        # 叠光标
        tmp_merage_2 = os.path.join(args.result_dir, "2_merage.mp4")
        render_video_with_cursor_from_json(
            video_path=tmp_merage_1, out_video_path=tmp_merage_2,
            json_path=cursor_save_path, cursor_img_path=cursor_img_path,
            transition_duration=0.1, cursor_size=max(1, round(size / 6 * args.cursor_scale)), cursor_hotspot=cursor_hotspot,
        )

        # 烧字幕（时间轴复用 cursor.json，无需二次转写）
        tmp_merage_3 = os.path.join(args.result_dir, "3_merage.mp4")
        add_subtitles(tmp_merage_2, tmp_merage_3, size // 10,
                      cursor_json_path=cursor_save_path,
                      font_name=lang["subtitle_font"])

        elapsed = time.time()
        time_second["merage"] = [elapsed - start_time]
        print("[Stage 3] 合片耗时 {:.1f}s -> {}".format(elapsed - start_time, tmp_merage_3))

    # ── 计时与 token 落盘 ──
    with open(sat_path, "w", encoding="utf-8") as handle:
        json.dump(time_second, handle, indent=4, ensure_ascii=False)
    with open(token_path, "w", encoding="utf-8") as handle:
        json.dump(token_usage, handle, indent=4, ensure_ascii=False)

    print("完成。结果目录: {}".format(args.result_dir))
