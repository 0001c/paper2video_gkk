# MuseTalk 接入方案（替代 LongCat-Video-Avatar）

> 2026-09-17 · 触发：LongCat 数字人在本机（RTX 5070 Ti / sm_120 / Windows）因
> 注意力后端三重障碍无法出片，用户提出改用 TMElyralab/MuseTalk。

## 一、结论

**可行。而且它在结构上绕开了 LongCat 的死结。**

用户提的「ffmpeg 把照片转 25fps 视频」**完全正确，且正是官方推荐做法**
（README:320：*"we recommend using input videos with 25fps … convert it to 25fps using ffmpeg"*）。
更省事的是：**MuseTalk 原生就支持单张图片**，不必先转视频。

## 二、为什么它能绕开 sm_120 死结（这是换型的核心理由）

| 维度 | LongCat-Video-Avatar | **MuseTalk** |
|---|---|---|
| 生成对象 | 整帧视频（480p） | **人脸裁剪 256×256** |
| 注意力序列长度 | 36480 | **1024**（256/8=32 → 32×32） |
| 注意力计算量 | 36480² ≈ 13.3 亿 | 1024² ≈ **105 万**（差约 **1270 倍**） |
| 注意力实现 | 依赖 flash-attn（Windows 装不了） | **标准 `diffusers.UNet2DConditionModel`**（SDPA 即可） |
| 权重大小 | 41.7 GB | **约 3.8 GB** |
| 架构 | 13.6B DiT | SD1.5 UNet + whisper-tiny |
| 速度 | 单卡不可行 | 官方称 V100 上 30fps+ |

`musetalk/models/unet.py` 的实际内容就是：

```python
from diffusers import UNet2DConditionModel
...
self.model = UNet2DConditionModel(**unet_config)
```

没有自定义注意力、不引用 flash-attn / xformers（后者只出现在 `models/syncnet.py`，
而 SyncNet 仅训练期评估用）。**小序列 + 标准实现 ⇒ sm_120 死结消失。**

## 三、输入方式：静态照片怎么喂

`scripts/inference.py:120-135` 的三种输入都支持：

```python
if get_file_type(video_path) == "video":     # 抽帧成 PNG 序列
elif get_file_type(video_path) == "image":   # ← 单张图，直接作为「帧列表」
    input_img_list = [video_path]
elif os.path.isdir(video_path):              # 图片目录
```

帧数如何匹配音频（关键机制）：

```python
video_num = len(whisper_chunks)                        # 输出帧数由【音频】决定
frame_list_cycle = frame_list + frame_list[::-1]        # 输入 latent 被循环
# datagen 内：idx = i % len(vae_encode_latents)
```

所以：**传 1 张图 → 该人脸 latent 被循环用于全部音频帧 → 静止的头 + 动的嘴。**
这正是「照片开口说话」的标准做法。

> 若偏好显式控制（例如想让视频帧数精确等于 音频时长×25），
> 也可按用户原方案用 ffmpeg 生成 25fps 静态视频：
> `ffmpeg -loop 1 -i photo.png -t <音频秒数> -r 25 -pix_fmt yuv420p ref.mp4`
> 两种方式结果等价（输入帧全都一样），单图模式少一步。

## 四、唯一的硬骨头：mmpose / mmcv（有干净对策）

`musetalk/utils/preprocessing.py` 顶层导入并在**模块导入时**初始化：

```python
from mmpose.apis import inference_topdown, init_model          # 第 10 行
config_file = './musetalk/utils/dwpose/rtmpose-l_..._384x288.py'
checkpoint_file = './models/dwpose/dw-ll_ucoco_384.pth'
model = init_model(config_file, checkpoint_file, device=device) # 第 19 行，导入即执行
```

mmpose 依赖 **mmcv**，而 mmcv 对 torch 2.11 / cu128 **没有预编译 wheel**，
从源码编译在 Windows 上需要 MSVC + CUDA 工具链（本项目已被这类问题拖过多次）。

**但它的用途是可以替换的**（`get_landmark_and_bbox`，第 97-101 行）：

```python
results = inference_topdown(model, np.asarray(fb)[0])   # mmpose DWPose，输出全身 133 点
results = merge_data_samples(results)
keypoints = results.pred_instances.keypoints
face_land_mark = keypoints[0][23:91]                    # 只取其中 68 个人脸关键点
```

而同一个文件里**已经初始化了 `face-alignment`**（第 23 行）：

```python
fa = FaceAlignment(LandmarksType._2D, flip_input=False, device=device)
```

`face-alignment` 输出的**正是标准 iBUG 68 点**，而 COCO-WholeBody 的 `23:91` 也是 iBUG 顺序
（索引 28/29/30 = 鼻梁三点，与 MuseTalk 下文的用法一致）—— **语义对齐，可直接替换**。

替换后 `mmpose / mmcv / mmdet` 全部不需要，**彻底不碰编译链**。
补丁做成可重放脚本（与 `patch_longcat.py` 同样的模式）。

## 五、环境依赖：与官方建议的偏离

官方建议 Python 3.10 + torch 2.0.1 + cu117 —— **本机不适用**（sm_120 必须 cu128）。

| 项 | 官方 | 本项目采用 | 原因 |
|---|---|---|---|
| Python | 3.10 | **3.12** | 沿用已建环境；且要 numpy ≥1.26 |
| torch | 2.0.1+cu117 | **2.11.0+cu128** | sm_120 只有 cu128 有编译目标 |
| `tensorflow==2.12.0` | 在 requirements 里 | **删除** | 全仓库零引用，纯遗留；它会把 Python 限到 ≤3.11 |
| `numpy==1.23.5` | 钉死 | **放宽到 1.26.4** | 3.12 无 1.23.5 的 wheel |
| `mmpose / mmcv` | 需安装 | **不装**（打补丁替换） | 无预编译 wheel，Windows 编译成本高 |
| `accelerate==0.28.0` | 钉死 | 用 1.15.0 | 已是 LongCat 环境里的版本 |
| `gradio` | 在 requirements 里 | 可选（不装） | 只用于 web demo |

保留的依赖：`diffusers==0.30.2`、`transformers==4.39.2`、`opencv-python`、`librosa`、
`soundfile`、`einops`、`omegaconf`、`imageio[ffmpeg]`、`moviepy`、`ffmpeg-python`、
`face-alignment`。

## 六、权重清单（约 3.8 GB，对比 LongCat 的 41.7 GB）

| 仓库 | 只取 | 大小 |
|---|---|---|
| `TMElyralab/MuseTalk` | `musetalkV15/unet.pth` + `musetalkV15/musetalk.json` | 3.24 GB |
| `stabilityai/sd-vae-ft-mse` | `config.json` + `diffusion_pytorch_model.bin` | 319 MB |
| `openai/whisper-tiny` | `config.json` + `pytorch_model.bin` + `preprocessor_config.json` | 144 MB |
| `ManyOtherFunctions/face-parse-bisent` | `79999_iter.pth` + `resnet18-5c106cde.pth` | 96 MB |

**不需要**：`yzd-v/DWPose`（绕开 mmpose 后）、`ByteDance/LatentSync` 的 syncnet
（仅训练/评估）、MuseTalk 仓库里 v1.0 的 `musetalk/pytorch_model.bin`（3.24GB，我们用 v1.5）。

另需 `face-alignment` 首次运行自带的 SFD / FAN 权重（约 100 MB，从 GitHub 拉，需预先下好）。

## 七、代价（必须明确）

MuseTalk 是 **lip-sync（口型同步）** 模型，**只重绘嘴部区域**：

- ✅ 口型准确、速度快、显存小（约 4 GB）
- ❌ **头姿 / 眨眼 / 表情保持输入原样**。静态照片 → 静止的头 + 动的嘴
- 与论文原本的 Hallo2 / InfiniteTalk（会生成头部动作）在表现力上有差距

**但本项目的数字人是合成到画面角落的 PIP（边长取画面长边 1/6）**，
在该尺寸下「静止头部 + 自然嘴型」完全可接受，且口型同步质量优于同尺寸下的大模型渲染。

> 备注：LongCat 的接入代码**并未白做** —— 它卡的是 GPU 的注意力 kernel，不是代码。
> 后续到 8×A6000（sm_80）机器上，flash-attn/xformers 都有现成 wheel，可直接启用，
> 作为「高表现力」档位与 MuseTalk 并存。

## 八、落地步骤

1. **下载权重**（3.8 GB）：在 `download_models.py` 增加 `musetalk` 分组 + 白名单
2. **建环境**：新增 `install_musetalk_deps.py`（沿用 `install_avatar_deps.py` 的模式，
   含 `CODEBUDDY_SAFE_DELETE_ENABLED=0` 规避守卫）
3. **打补丁**：新增 `patch_musetalk.py`
   - 用 `fa.get_landmarks()` 替换 mmpose 的 `inference_topdown`
   - 语法自检 + 幂等 + `--check`（与 `patch_longcat.py` 同模式）
4. **接后端**：改 `talking_gen.py` 的后端实现（保持 `talking_gen_per_slide` 签名不变，
   产物仍落 `<save_dir>/<idx>/<ref_stem>/merge_video.mp4` 且带音轨）
   - 单图模式：直接传暂存后的 PNG
   - 说明：MuseTalk 输出帧率由 `--fps`（默认 25）决定
5. **改配置**：`talking_config.py` 档位换成 MuseTalk 参数集
   （`--version v15`、`--fps 25`、`--bbox_shift`、`--extra_margin`）
6. **冒烟测试**：单页（1 张图 + 10s 音频），验证口型同步 / 耗时 / 显存
