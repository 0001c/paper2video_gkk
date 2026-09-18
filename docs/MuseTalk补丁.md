# MuseTalk 补丁：强制偶数尺寸（否则数字人整批产出为空）

## 症状（这版最容易误判的地方）

数字人阶段"跑完了"，但**每一页都没有产物**：

```
[talking_gen] 有 11 页失败：
  第 0 页：批量跑完但第 0 页没有产物（预期 ...\vid_output\page_000.mp4）
  ...（11 页全一样）
Talking Generation 290.9017369747162          ← 耗时正常，看着像跑成功了
```

日志里往前翻，真正的错因只有几行，且很容易被漏掉：

```
[libx264 @ ...] width not divisible by 2 (215x221)
[vost#0:0/libx264] Error while opening encoder - maybe incorrect parameters
[out#0/mp4] Nothing was written into output file, because at least one of its streams received no packets.
[in#1] moov atom not found                     ← 下一步读 temp.mp4 时才暴露
Error opening input file ./results/.../temp.mp4
```

**三层误导**：
1. 报错点在**下一步**（合音频）而不是出错的那一步；
2. `result is save to .../page_010.mp4` 照样打印（上游只在 ffmpeg 之后无条件打印）；
3. 总耗时正常，看起来像"模型跑完了但产物被弄丢了"。

## 根因

上游把帧序列编码成视频时用的是：

```python
cmd_img2video = f"ffmpeg -y -v warning -r {fps} -f image2 -i {avatar_path}/tmp/%08d.png \
-vcodec libx264 -vf format=yuv420p -crf 18 {avatar_path}/temp.mp4"
```

**libx264 的 `yuv420p` 要求宽高都是偶数**。而人脸裁框尺寸来自检测框，可能是奇数
（本次实测 `215x221`）→ 编码器初始化失败 → `temp.mp4` 是 **0 字节** →
合音频时读不进去（`moov atom not found`）→ 该页没有产物。

**为什么"昨天还能跑"**：裁框奇偶取决于**参考图**。同一套代码，
换一张人像就可能从偶数变成奇数。实测 `assets/gkk/portrait.png` 一路正常，
换成一张截图（人脸占比小、裁框 215×221）就 11 页全废。

## 补丁

给滤镜链加 `crop=trunc(iw/2)*2:trunc(ih/2)*2`（向下取偶）。

⚠️ **引号是个坑**：那条命令是**双引号定界的 f-string**，
所以注入的双引号在源码里必须写成 `\"`；而 `os.system` 走 cmd.exe，
**单引号在 cmd 下不是引号字符**，不能用单引号。

```diff
- cmd_img2video = f"ffmpeg ... -vcodec libx264 -vf format=yuv420p -crf 18 ..."
+ cmd_img2video = f"ffmpeg ... -vcodec libx264 -vf \"format=yuv420p,crop=trunc(iw/2)*2:trunc(ih/2)*2\" -crf 18 ..."
```

需要改两个文件（两个入口都用了同一段）：

| 文件 | 位置 |
|---|---|
| `<MuseTalk>/scripts/realtime_inference.py` | 批量入口（本项目走的是这个）|
| `<MuseTalk>/scripts/inference.py` | 单页入口（备用）|

改完**必须用 MuseTalk 自己的解释器做语法检查**（f-string 引号写错会直接 `SyntaxError`，
而它表现为"退出码 1、11 页全失败"，又是一次误导）：

```bat
<MuseTalk 的解释器> -m py_compile <MuseTalk>\scripts\realtime_inference.py
```

## 验证（A/B，不用真跑一遍模型）

造几张奇数尺寸帧，把同一条命令跑两遍：

```
① 不加 crop：退出码 -542398533，temp.mp4 0 字节        ← 复现故障
② 加 crop  ：退出码 0，产物 214×220、h264 视频 + aac 音频
```

更稳的验证方式是**从补丁后的源码里取出那一行，求值后执行**
（避免手写命令时在多层转义里测出假结果 —— 这一点已经栽过两次）。
脚本见 `scripts/verify_musetalk_patch.py`。

## 迁移提醒（学校 Linux 服务器）

这份补丁改的是**上游仓库**，不在本仓库的 Git 里。
在服务器上重新 clone MuseTalk 之后**必须重新打一次**，
否则只要那台机器上的人像裁框是奇数，数字人就会静默产出为空。

程序侧已加自检：`talking_gen.py` 在进入批量入口前会检查该文件是否含 `crop=trunc(iw/2)`，
缺失就打一段醒目的提示（含具体改法与本文档路径）。它**只提示、不中断** ——
裁框恰好是偶数时上游原样也能跑通，不该因此挡住正常流程。

## 相关：另一个"最后一步崩掉"的隐患（同批修掉）

`pipeline.py` 在合片前需要读一张幻灯片的尺寸：

```python
image_size = cv2.imread(path.join(slide_image_dir, '1.png')).shape   # 旧
```

实测 `cv2.imread` 会**偶发返回 None**（OpenCV 自己打一行
`cv2::findDecoder imread_('<路径>')` 就放弃；同一条路径换 PIL / PyMuPDF 能正常打开）。
这一行位于流水线**最末端**，`.shape` 一崩，20 分钟的活儿全废在最后一步。

已改为 `read_image_size()`：cv2 → PIL → PyMuPDF 依次尝试，
全部失败才抛错，并把每种读取器的失败原因写进报错信息。
