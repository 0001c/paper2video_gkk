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

`face_alignment/api.py:118-143` 会调 `torch.compile` 加速。它虽然用 try/except 包了，
但 **`torch.compile` 是惰性的**：构造时不报错，真正编译发生在首次 forward（try 之外）。
本机拿不到 `cl.exe` + MSVC 环境，于是崩在推理时：

    InductorError: Compiler: cl is not found.

关掉 dynamo 后 `torch.compile` 退化为 no-op，走 eager。FAN 只跑 256x256，
eager 速度完全够。

## 用法

    ./.venv/Scripts/python.exe scripts/patch_musetalk.py           # 打补丁
    ./.venv/Scripts/python.exe scripts/patch_musetalk.py --check   # 只看状态
"""

import argparse
import ast
import os
import sys

DEFAULT_ROOT = os.environ.get("MUSETALK_ROOT", r"E:\ai-models\MuseTalk")

REL_PREPROCESSING = os.path.join("musetalk", "utils", "preprocessing.py")


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
# ③ 禁用 torch dynamo：face_alignment 会 torch.compile 加速，而编译需要 cl.exe；
#    其 try/except 兜不住（compile 惰性，真正编译在首次 forward）。关掉走 eager。
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
    ok = True
    touched = set()

    print()
    print("[1/4] 头部：face_detection -> face-alignment；移除 mmpose；禁用 dynamo")
    state = _apply(target, HEADER_OLD, HEADER_NEW, "头部导入", dry, touched=touched)
    print("      {}  {}".format(state, REL_PREPROCESSING))
    if state not in ("已打过", "已改写 x1", "需要改写 x1"):
        ok = False

    print()
    print("[2/4] 关键点：mmpose inference_topdown -> face-alignment get_landmarks（2 处）")
    state = _apply(target, LANDMARK_OLD, LANDMARK_NEW, "关键点提取", dry,
                   all_occurrences=True, touched=touched)
    print("      {}".format(state))
    if state not in ("已打过", "已改写 x2", "需要改写 x2"):
        ok = False

    print()
    print("[3/4] 检测框：get_detections_for_batch -> get_landmarks 的返回值（2 处）")
    state = _apply(target, BBOX_OLD, BBOX_NEW, "检测框来源", dry,
                   all_occurrences=True, touched=touched)
    print("      {}".format(state))
    if state not in ("已打过", "已改写 x2", "需要改写 x2"):
        ok = False

    print()
    print("[4/4] 兜底：关键点缺失时退回检测框（2 处）")
    state = _apply(target, GUARD_OLD, GUARD_NEW, "无关键点兜底", dry,
                   all_occurrences=True, touched=touched)
    print("      {}".format(state))
    if state not in ("已打过", "已改写 x2", "需要改写 x2"):
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
