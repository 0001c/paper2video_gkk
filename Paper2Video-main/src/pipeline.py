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
from pdf_render import render_pdf_to_images

from speech_gen import tts_per_slide
from subtitle_render import add_subtitles
from talking_gen import talking_gen_per_slide
from merge_talking import merge_slides_and_talking
from talking_config import describe_profile, resolve_profile
from cursor_gen import cursor_gen_per_sentence
# from slide_code_gen import latex_code_gen
from slide_code_gen_select_improvement import latex_code_gen_upgrade
from cursor_render import render_video_with_cursor_from_json
from subtitle_cursor_prompt_gen import subtitle_cursor_gen

from wei_utils import get_agent_config


# os.environ["GEMINI_API_KEY"] = ""
# os.environ["OPENAI_API_KEY"] = ""

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
    parser.add_argument('--model_name_talking', type=str, default='musetalk')
    parser.add_argument('--talking_profile', type=str, default=None,
                        help='数字人运行档位（见 talking_config.list_profiles()）：'
                             'musetalk-local 本机单卡 / musetalk-server 预留多卡；'
                             'longcat 后端另有 local-16g / server-8xa6000')
    parser.add_argument('--paper_latex_root', type=str, default='./assets/demo/latex_proj')
    parser.add_argument('--ref_img', type=str, default='./assets/demo/zeyu.png')
    parser.add_argument('--ref_audio', type=str, default='./assets/demo/zeyu.wav')
    parser.add_argument('--ref_text', type=str, default=None)
    parser.add_argument('--gpu_list', type=str2list, default="")
    parser.add_argument('--if_tree_search', type=str2bool, default=True)
    parser.add_argument('--beamer_templete_prompt', type=str, default=None)
    parser.add_argument('--stage', type=str, default="[\"0\"]") 
    parser.add_argument('--talking_head_env', type=str, default="") 
    # slide+subtitle: 1; 
    # tts+cusor: 2; 
    # talking-head: 3: 
    # all: 0
    args = parser.parse_args()
    stage = json.loads(args.stage)
    print("start", "stage:", stage, args.gpu_list)
    
    cursor_img_path = "./cursor_image/red.png"
    os.makedirs(args.result_dir, exist_ok=True) # result dir
    agent_config_t = get_agent_config(args.model_name_t) # LLM
    agent_config_v = get_agent_config(args.model_name_v) # VLM
    copy_latex_proj_path = path.join(args.result_dir, path.basename(args.paper_latex_root))
    if path.exists(copy_latex_proj_path) is False:
        copy_folder(args.paper_latex_root, copy_latex_proj_path)
    args.paper_latex_root = copy_latex_proj_path
    
    if path.exists(path.join(args.result_dir, "sat.json")) is True:
        with open(path.join(args.result_dir, "sat.json"), 'r') as f: 
            time_second = json.load(f)
    else: time_second = {}
        
    if path.exists(path.join(args.result_dir, "token.json")) is True:
        with open(path.join(args.result_dir, "token.json"), 'r') as f: 
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
    os.makedirs(slide_image_dir, exist_ok=True)
    
    start_time = time.time() # start time
    if "1" in stage or  "0" in stage:
        prompt_path = "./prompts/slide_beamer_prompt.txt"
        if args.if_tree_search is True: 
            usage_slide, beamer_path = latex_code_gen_upgrade(prompt_path=prompt_path, tex_dir=args.paper_latex_root, beamer_save_path=slide_latex_path, 
                                                            model_config_ll=agent_config_t, model_config_vl=agent_config_v, beamer_temp_name=args.beamer_templete_prompt,
                                                            if_tree_search=args.if_tree_search)
        else:
            paper_latex_path = path.join(args.paper_latex_root, "main.tex") 
            usage_slide = latex_code_gen(prompt_path=prompt_path, tex_dir=args.paper_latex_root, tex_path=paper_latex_path, beamer_save_path=slide_latex_path, model_config=agent_config_t)
            
        # 上游这里用 pdf2image.convert_from_path，但它依赖 poppler（Windows 上要额外装），
        # 而仓库自带的 pdf2img() 本来就是 PyMuPDF 实现。统一改走 PyMuPDF，免装 poppler。
        slide_imgs = render_pdf_to_images(beamer_path, dpi=400)
        for i, img in enumerate(slide_imgs): img.save(path.join(slide_image_dir, f"{i+1}.png")) # save slides as images
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
        prompt_path = "./prompts/slide_subtitle_cursor_prompt.txt"
        subtitle, usage_subtitle = subtitle_cursor_gen(slide_image_dir, prompt_path, agent_config_v)
        with open(subtitle_cursor_save_path, 'w') as f: f.write(subtitle)
        if args.model_name_v not in token_usage.keys(): 
            token_usage[args.model_name_v] = [usage_subtitle]
        else: token_usage[args.model_name_v].append(usage_subtitle)
        step2_time =  time.time()
        time_second["subtitle_cursor_prompt_gen"] = [step2_time-start_time]
        print("Subtitle and Cursor Prompt Generation", step2_time-start_time)

        ## Step 3-1: Speech Generation
        tts_per_slide(model_type='f5', script_path=subtitle_cursor_save_path, 
                    speech_save_dir=speech_save_dir, ref_audio=args.ref_audio, ref_text=args.ref_text)  
        step3_1_time =  time.time()
        time_second["tts"] = [step3_1_time-step2_time]
        print("Speech Generation", step3_1_time-step2_time)
        
        ## Step 3-2: Cursor Generation
        os.environ["PYTHONHASHSEED"] = "random"        
        cursor_token = cursor_gen_per_sentence(script_path=subtitle_cursor_save_path, slide_img_dir=slide_image_dir, 
                                slide_audio_dir=speech_save_dir, cursor_save_path=cursor_save_path, gpu_list=args.gpu_list)
        token_usage["cursor"] = cursor_token
        step3_2_time =  time.time()
        time_second["cursor_gen"] = [step3_2_time-step3_1_time]
        print("Cursor Generation", step3_2_time-step3_1_time)
    
    ## Step 4: Talking Video Generation（数字人）
    start_time = time.time() # start time
    if "3" in stage or  "0" in stage:
        print("[数字人] 档位：{}".format(describe_profile(
            resolve_profile(args.talking_profile, backend=args.model_name_talking))))
        talking_save_dir = path.join(args.result_dir, 'talking_{}'.format(args.model_name_talking))
        talking_inference_input = []
        # ⚠️ 必须按**数字**排序：文件名是 0.wav..11.wav，而 os.listdir 顺序不保证，
        # 且字典序会把 10.wav / 11.wav 排到 2.wav 前面。一旦顺序错位，数字人就会
        # 对错音频，而症状是「口型与语音不符」——很难联想到是排序问题。
        audio_names = [n for n in os.listdir(speech_save_dir) if n.lower().endswith(".wav")]
        audio_names = sorted(audio_names, key=lambda x: int(re.search(r"\d+", x).group()))
        for name in audio_names:
            talking_inference_input.append([args.ref_img, path.join(speech_save_dir, name)])
        talking_gen_per_slide(args.model_name_talking, talking_inference_input, talking_save_dir,
                              args.gpu_list, env_path=args.talking_head_env,
                              profile=args.talking_profile)
        step4_time =  time.time()
        time_second["talking_gen"] = [step4_time-start_time]
        print("Talking Generation", step4_time-start_time)
    
        ## Step5: Merage
        # 上游调 ./1_merage.bash（Windows 不可执行，且把 list.txt 写在 cwd），改走 Python
        tmp_merage_dir = path.join(args.result_dir, "merage")
        tmp_merage_1 = path.join(args.result_dir, "1_merage.mp4")
        image_size = cv2.imread(path.join(slide_image_dir, '1.png')).shape
        # 数字人嵌在右上角，边长取画面长边的 1/6（沿用上游 hallo2 的取法，保证版式一致）
        size = max(image_size[0]//6, image_size[1]//6)
        width, height = size, size
        num_slide = len([n for n in os.listdir(slide_image_dir) if n.lower().endswith(".png")])
        ref_stem = path.splitext(path.basename(args.ref_img))[0]
        merge_slides_and_talking(
            slide_dir=slide_image_dir, talking_dir=talking_save_dir,
            output_dir=tmp_merage_dir, video_path=tmp_merage_1,
            num_video=num_slide, ref_img=ref_stem, width=width, height=height)
        # render cursor
        cursor_size = size//6
        tmp_merage_2 = path.join(args.result_dir, "2_merage.mp4")
        render_video_with_cursor_from_json(video_path=tmp_merage_1, out_video_path=tmp_merage_2, 
                                        json_path=cursor_save_path, cursor_img_path=cursor_img_path, 
                                        transition_duration=0.1, cursor_size=cursor_size)
        # render subtitle
        front_size = size//10
        tmp_merage_3 = path.join(args.result_dir, "3_merage.mp4")
        add_subtitles(tmp_merage_2, tmp_merage_3, size//10)
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
    with open(path.join(args.result_dir, "sat.json"), 'w') as f: json.dump(time_second, f, indent=4)
    with open(path.join(args.result_dir, "token.json"), 'w') as f: json.dump(token_usage, f, indent=4)
