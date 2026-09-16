#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""给 MuseTalk 打本机（Windows / Blackwell sm_120）适配补丁。

## 为什么需要补丁

MuseTalk 的 `musetalk/utils/preprocessing.py` 依赖两个在本机拿不到的东西：

**① `from face_detection import FaceAlignment, LandmarksType` 无法被满足**

我把 PyPI 上 `face-detection` 的所有版本都拆开看过：

| 版本 | `__init__.py` 导出 | 有 FaceAlignment？ |
|---|---|---|
| 0.1.1 | `detect_faces, DSFDDetector` | ❌ |
| 0.1.4 | `build_detector, available_detectors, DSFDDetector, RetinaNet*` | ❌ |
| 0.2.0–0.2.2 | `Detector` / 各检测器（且 0.2.2 无 wheel，仅 sdist） | ❌ |

也就是说**任何已发布版本都不含 `FaceAlignment`**（该包只做检测）。
而 `face-alignment` 1.5.0（其正牌后继）提供 `FaceAlignment` + `LandmarksType`，
并且一次调用同时给出 **68 点关键点与检测框**，正好覆盖 MuseTalk 的两处用途。

**② mmpose（DWPose）依赖 mmcv**

mmcv 对 torch 2.11 / cu128 **没有预编译 wheel**，Windows 上从源码编译需要
MSVC + CUDA 工具链（本项目已被同类问题拖过多次）。

而 mmpose 在这份代码里只做一件事：从全身 133 点里切出 68 个人脸点
（`keypoints[0][23:91]`）。`face-alignment` 的 FAN 模型输出的**正是标准 iBUG 68 点**，
实测索引 28/29/30 为同一竖线上的鼻梁三点（x 完全相同、y 递增），
与上游用法（`face_land_mark[28/29/30]`）语义一致 —— **可直接替换**。

**③ 需要禁用 torch dynamo**

`face_alignment/api.py:118-143` 会调 `torch.compile` 加速，并当场做一次暖机
forward。本机拿不到 `cl.exe` + MSVC 环境，于是崩在 inductor 的 C++ 编译阶段：

    torch._inductor.exc.InductorError: RuntimeError: Compiler: cl is not found.

⚠️ 这段代码外面是包了 `try/except Exception` 的（`api.py:142`），按说
`InductorError` 继承自 `Exception`、应该被兜住并降级到 eager。**但实测没有**，
进程直接退出，且 `logger.warning('torch.compile failed ...')` 也没有出现。
具体是 inductor 的异步编译线程把异常抛在了别的栈上，还是别的原因，未深究 ——
本补丁不去赌它，直接把 dynamo 关掉，让 `torch.compile` 退化为 no-op。
FAN 只跑 256x256，eager 速度完全够。

**④ `torch.load` 的 `weights_only` 默认值翻转 —— 不由本补丁处理**

PyTorch 2.6 起 `torch.load` 的 `weights_only` 默认从 False 变为 True，
而 MuseTalk 有三处调用没显式传该参数，其中 `resnet18-5c106cde.pth` 是
legacy `.tar` 格式，会直接报：

    RuntimeError: Cannot use ``weights_only=True`` with files saved in
    the legacy .tar format.

这里**故意不改代码**，而是在 `talking_gen.py` 里给 MuseTalk 子进程设
`TORCH_FORCE_NO_WEIGHTS_ONLY_LOAD=1`（torch 官方开关，
`serialization.py:1486`，且只在调用点没显式传参时生效）。理由：4 处调用点分散在
第三方文件里，改代码的收益不如一个环境变量，且不会与上游更新打架。

## 用法

    ./.venv/Scripts/python.exe scripts/patch_musetalk.py           # 打补丁
    ./.venv/Scripts/python.exe scripts/patch_musetalk.py --check   # 只看状态
"""

import argparse
import ast
import os
import re
import sys

DEFAULT_ROOT = os.environ.get("MUSETALK_ROOT", r"E:\ai-models\MuseTalk")

REL_PREPROCESSING = os.path.join("musetalk", "utils", "preprocessing.py")
# 补丁 E 的目标是推理入口（不是 preprocessing）：save_dir_full 的问题在那边
REL_INFERENCE = os.path.join("scripts", "inference.py")

# 头部补丁的稳定标记：判断「已打过」只看这一行，不看整段说明
# （理由见 _apply_header 的 docstring）
HEADER_MARKER = "# 本机补丁：由 scripts/patch_musetalk.py 生成，勿手改。"

# 头部说明块的形状：`# ──...` 起、标记行、任意内容、`# ──...` 收
HEADER_BLOCK_RE = re.compile(
    r"# ─+\n" + re.escape(HEADER_MARKER) + r".*?\n# ─+\n", re.S)


# ── 补丁 A：头部导入与初始化（1 处，非幂等替换）────────────────────
HEADER_OLD = """from face_detection import FaceAlignment,LandmarksType
from os import listdir, path
import subprocess
import numpy as np
import cv2
import pickle
import os
import json
from mmpose.apis import inference_topdown, init_model
from mmpose.structures import merge_data_samples
import torch
from tqdm import tqdm

# initialize the mmpose model
device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
config_file = './musetalk/utils/dwpose/rtmpose-l_8xb32-270e_coco-ubody-wholebody-384x288.py'
checkpoint_file = './models/dwpose/dw-ll_ucoco_384.pth'
model = init_model(config_file, checkpoint_file, device=device)

# initialize the face detection model
device = "cuda" if torch.cuda.is_available() else "cpu"
fa = FaceAlignment(LandmarksType._2D, flip_input=False,device=device)
"""

HEADER_NEW = """# ─────────────────────────────────────────────────────────────────────
# 本机补丁：由 scripts/patch_musetalk.py 生成，勿手改。三处改动：
#
# ① `from face_detection import FaceAlignment, LandmarksType` 无法满足：
#    face-detection 0.1.x 只导出 DSFD/RetinaFace、0.2.x 只导出 Detector，
#    任何已发布版本都不含 FaceAlignment。改用 face-alignment 1.5.0。
# ② 去掉 mmpose（DWPose）：它依赖 mmcv，而 mmcv 对 torch 2.11/cu128 无预编译
#    wheel。face-alignment 的 FAN 输出的正是标准 iBUG 68 点，语义一致。
# ③ 禁用 torch dynamo：face_alignment 会 torch.compile 加速，而 inductor 的
#    C++ 编译需要 cl.exe（本机无 MSVC）。上游虽然用 try/except 包了，**实测
#    兜不住**，进程直接退出。关掉 dynamo 后退化为 eager，FAN 只跑 256x256 够用。
#
# 另有第 ④ 项不兼容（torch.load 的 weights_only 默认值翻转），**不在此处改代码**，
# 见 scripts/patch_musetalk.py 头部说明 —— 走环境变量
# TORCH_FORCE_NO_WEIGHTS_ONLY_LOAD=1，由 talking_gen.py 设置。
# ─────────────────────────────────────────────────────────────────────
from face_alignment import FaceAlignment, LandmarksType
from os import listdir, path
import subprocess
import numpy as np
import cv2
import pickle
import os
import json
import torch
from tqdm import tqdm

torch._dynamo.config.disable = True

# 原 mmpose 初始化已移除（见上 ②）
device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

# initialize the face detection model
device = "cuda" if torch.cuda.is_available() else "cpu"
# 注：LandmarksType._2D -> TWO_D（face-alignment 1.5 的枚举名）
fa = FaceAlignment(LandmarksType.TWO_D, flip_input=False, device=device)
"""

# ── 补丁 B：关键点提取（2 处，内容相同 -> 全局替换）────────────────
LANDMARK_OLD = """        results = inference_topdown(model, np.asarray(fb)[0])
        results = merge_data_samples(results)
        keypoints = results.pred_instances.keypoints
        face_land_mark= keypoints[0][23:91]
        face_land_mark = face_land_mark.astype(np.int32)
"""

LANDMARK_NEW = """        # face-alignment 一次给出 68 点与检测框；返回三元组
        # (landmark, landmark_score, detected_face)（见 face_alignment/api.py:200-208）
        _lms, _score, _boxes = fa.get_landmarks(np.asarray(fb)[0], return_bboxes=True)
        face_land_mark = None if _lms is None else np.asarray(_lms[0]).astype(np.int32)
"""

# ── 补丁 C：检测框来源（2 处）──────────────────────────────────────
BBOX_OLD = "        bbox = fa.get_detections_for_batch(np.asarray(fb))\n"

BBOX_NEW = """        # 检测框由上面 get_landmarks(return_bboxes=True) 一并给出
        bbox = ([None] if face_land_mark is None
                else [np.asarray(_boxes[0][:4]).astype(np.int32)])
"""

# ── 补丁 D：无关键点时的兜底（2 处）────────────────────────────────
GUARD_OLD = """        for j, f in enumerate(bbox):
            if f is None: # no face in the image
                coords_list += [coord_placeholder]
                continue
"""

GUARD_NEW = """        for j, f in enumerate(bbox):
            if f is None: # no face in the image
                coords_list += [coord_placeholder]
                continue
            if face_land_mark is None: # 关键点缺失：退回人脸检测框
                coords_list += [f]
                continue
"""

# ── 补丁 E：图片输入路径上的 save_dir_full 未定义（2 处）────────────
# inference.py 只在 `get_file_type(video_path) == "video"` 分支里给
# save_dir_full 赋值（第 121 行），但收尾清理（第 243 行）无条件 rmtree 它。
# 而我们走的是**图片输入**，于是每次都在收尾时报：
#     Error occurred during processing: cannot access local variable
#     'save_dir_full' where it is not associated with a value
#
# 后果不算致命（产物 mp4 在第 235 行已写好），但有三点实际危害：
#   ① 该异常被第 248 行的 `except Exception` 吞掉，**退出码仍是 0** ——
#      于是「退出码」这个健康信号被污染，真错误可能与它混在一起看不出来；
#   ② 第 245 行 os.remove(crop_coord_save_path) 与第 247 行的
#      "Results saved to ..." 都被跳过，工作区里每次残留一个 ref_image.pkl；
#   ③ 日志里出现一行 "Error occurred"，排查时很误导。
# 注：这是上游 bug（作者主要按视频输入测的），不是本机环境问题。
SAVE_DIR_OLD = """            if get_file_type(video_path) == "video":
                save_dir_full = os.path.join(temp_dir, input_basename)
"""

SAVE_DIR_NEW = """            # 本机补丁：图片/图片目录输入时下面这个分支不会执行，
            # 而收尾清理会无条件用它 —— 先给个缺省值（见 patch_musetalk.py 补丁 E）
            save_dir_full = None
            if get_file_type(video_path) == "video":
                save_dir_full = os.path.join(temp_dir, input_basename)
"""

CLEANUP_OLD = "            shutil.rmtree(save_dir_full)\n"

CLEANUP_NEW = """            # 本机补丁：图片输入时 save_dir_full 从未赋值，只有真解压过帧才需要删
            if save_dir_full:
                shutil.rmtree(save_dir_full)
"""


def _apply(path, old, new, desc, dry, all_occurrences=False, touched=None):
    """做一次文本替换；返回状态字符串。"""
    if not os.path.isfile(path):
        return "文件不存在"
    text = open(path, encoding="utf-8").read()
    # 先判「新」再判「旧」：new 含 old 时判定才不会失效
    if new in text:
        return "已打过"
    count = text.count(old)
    if count == 0:
        return "未找到预期文本（上游可能已改版）"
    if all_occurrences is False and count > 1:
        return "意外出现 {} 次（应为 1）".format(count)
    if dry:
        return "需要改写 x{}".format(count)
    text = text.replace(old, new) if all_occurrences else text.replace(old, new, 1)
    open(path, "w", encoding="utf-8", newline="").write(text)
    if touched is not None:
        touched.add(path)
    return "已改写 x{}".format(count)


def _apply_header(path, dry, touched=None):
    """头部补丁（补丁 A），用**稳定标记**判「已打过」，并支持刷新说明文字。

    ⚠️ 为什么不能像补丁 B/C/D 那样直接 `_apply(HEADER_OLD, HEADER_NEW)`：
    那种判据是「HEADER_NEW 全文是否已在文件里」。而 HEADER_NEW 开头那段中文
    说明会随本项目演进而调整（例如第 ③ 条的机理描述被修正过），一旦改动，
    已经打过补丁的仓库就会同时匹配不上 HEADER_OLD 与新的 HEADER_NEW，于是
    被**误报成「未找到预期文本（上游可能已改版）」并让整个补丁返回非 0** ——
    很容易让人以为补丁坏了而去查上游。

    改为：以 `# 本机补丁：由 scripts/patch_musetalk.py 生成，勿手改。` 这一行
    作为「已打过」的判据；若说明块与当前 HEADER_NEW 不一致，就地刷新它，
    这样修正过的说明也能落到仓库里。
    """
    if not os.path.isfile(path):
        return "文件不存在"
    text = open(path, encoding="utf-8").read()

    wanted = HEADER_BLOCK_RE.search(HEADER_NEW)
    if wanted is None:
        raise AssertionError("HEADER_NEW 里找不到头部说明块，正则需同步更新")
    wanted_block = wanted.group(0)

    if HEADER_MARKER in text:
        current = HEADER_BLOCK_RE.search(text)
        if current is not None and current.group(0) == wanted_block:
            return "已打过"
        if current is None:
            return "已打过（但头部说明块形状异常，无法刷新）"
        if dry:
            return "已打过（说明文字已过期，需要刷新）"
        open(path, "w", encoding="utf-8", newline="").write(
            text.replace(current.group(0), wanted_block, 1))
        if touched is not None:
            touched.add(path)
        return "已打过（说明文字已刷新）"

    return _apply(path, HEADER_OLD, HEADER_NEW, "头部导入", dry, touched=touched)


def main():
    parser = argparse.ArgumentParser(description="给 MuseTalk 打本机适配补丁")
    parser.add_argument("--root", default=DEFAULT_ROOT, help="MuseTalk 仓库根目录")
    parser.add_argument("--check", action="store_true", help="只检查，不写入")
    args = parser.parse_args()

    root, dry = args.root, args.check
    print("MuseTalk 仓库：{}".format(root))
    if not os.path.isdir(root):
        print("目录不存在。请先克隆 TMElyralab/MuseTalk 并重命名为 MuseTalk。")
        return 2

    target = os.path.join(root, REL_PREPROCESSING)
    target_infer = os.path.join(root, REL_INFERENCE)
    ok = True
    touched = set()

    print()
    print("[1/5] 头部：face_detection -> face-alignment；移除 mmpose；禁用 dynamo")
    state = _apply_header(target, dry, touched=touched)
    print("      {}  {}".format(state, REL_PREPROCESSING))
    if not state.startswith("已打过") and state not in ("已改写 x1", "需要改写 x1"):
        ok = False

    print()
    print("[2/5] 关键点：mmpose inference_topdown -> face-alignment get_landmarks（2 处）")
    state = _apply(target, LANDMARK_OLD, LANDMARK_NEW, "关键点提取", dry,
                   all_occurrences=True, touched=touched)
    print("      {}".format(state))
    if state not in ("已打过", "已改写 x2", "需要改写 x2"):
        ok = False

    print()
    print("[3/5] 检测框：get_detections_for_batch -> get_landmarks 的返回值（2 处）")
    state = _apply(target, BBOX_OLD, BBOX_NEW, "检测框来源", dry,
                   all_occurrences=True, touched=touched)
    print("      {}".format(state))
    if state not in ("已打过", "已改写 x2", "需要改写 x2"):
        ok = False

    print()
    print("[4/5] 兜底：关键点缺失时退回检测框（2 处）")
    state = _apply(target, GUARD_OLD, GUARD_NEW, "无关键点兜底", dry,
                   all_occurrences=True, touched=touched)
    print("      {}".format(state))
    if state not in ("已打过", "已改写 x2", "需要改写 x2"):
        ok = False

    print()
    print("[5/5] 图片输入路径：save_dir_full 未定义（上游 bug，2 处）")
    state = _apply(target_infer, SAVE_DIR_OLD, SAVE_DIR_NEW, "save_dir_full 初始化",
                   dry, all_occurrences=False, touched=touched)
    print("      初始化    {}  {}".format(state, REL_INFERENCE))
    if state not in ("已打过", "已改写 x1", "需要改写 x1"):
        ok = False
    state = _apply(target_infer, CLEANUP_OLD, CLEANUP_NEW, "清理判空",
                   dry, all_occurrences=False, touched=touched)
    print("      清理判空  {}".format(state))
    if state not in ("已打过", "已改写 x1", "需要改写 x1"):
        ok = False

    # 语法自检：文本替换很容易把缩进改坏，必须当场拦住
    print()
    print("[自检] 被改动文件的语法")
    for path in sorted(touched) or [target]:
        try:
            ast.parse(open(path, encoding="utf-8").read())
            print("      [OK] {}".format(os.path.relpath(path, root)))
        except SyntaxError as exc:
            print("      [语法错误] {}:{}: {}".format(
                os.path.relpath(path, root), exc.lineno, exc.msg))
            ok = False

    print()
    if dry:
        print("（--check 模式：未写入任何文件）")
    else:
        print("补丁完成。" if ok else "补丁部分失败，请查看上面的提示。")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
