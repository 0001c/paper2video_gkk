# Paper2Video项目汇报（演示文稿工程）

面向同学与老师的课堂汇报，15 页。内容依据 `../docs/代码改造总览.md`。

```
STORY.md                       叙事稿：意图对齐 / 页面骨架 / 逐页大纲
ASSETS.md                      素材清单（24 项，全部来自项目真实产物）
slides/01..15.slide            页面源文件
assets/                        页面引用的图片（ASCII 命名）
backup/                        改 pptx 前的备份
fix_chart_axes.py              修图表轴位的后处理脚本（见下）
Paper2Video项目汇报.pptx       成品
```

---

## ⚠️ 生成后必须跑一次 `fix_chart_axes.py`

**这不是可选项。** 页面源文件（`.slide`）**无法表达图表轴的位置**，转换器又有个缺陷：

> 转换器写 `<c:axPos>` 时只按**柱状图**那套写死 `分类轴=b(下) / 数值轴=l(左)`，
> **完全没看 `barDir`**。对 `barDir="col"`（竖向柱）恰好正确；
> 对 `barDir="bar"`（横向条）必须反过来 —— `分类轴=l(左) / 数值轴=b(下)`。

后果：横向条形图的**分类名与数值刻度互换**，看起来像"标签和轴不对应"。

| 页 | 图表 | barDir | 转换器写出 | 应为 | 结果 |
|---|---|---|---|---|---|
| 03 | 显存规模对比 | `bar` | b / l | l / b | 曾互换（已修） |
| 08 | token 构成 | `col` | b / l | b / l | 正常 |
| 11 | 各阶段耗时 | `bar` | b / l | l / b | 曾互换（已修） |

```bash
# 只需一条命令；脚本可重复执行（已正确则跳过），并自动备份到 backup/
.venv/Scripts/python.exe fix_chart_axes.py
```

**判据**：跑完应看到

```
chart1.xml  barDir=bar → 期望 catAx=l / valAx=b
   catAx b -> l   valAx l -> b
chart2.xml  barDir=bar → 期望 catAx=l / valAx=b
   catAx b -> l   valAx l -> b
```

即**只交换 2 个属性值**。交换是等长的，所以文件字节数不变 —— 可以用这个佐证"只动了这一处"。

若哪天再用 slidep 重新生成第 3 页或第 11 页，**必须重跑这个脚本**。

---

## 编辑这份 pptx 时踩到的两个环境坑

1. **改图表轴位不能用编辑器通道。** 本地 editor_sdk 的 `slide_update_chart_axis` 只支持
   轴可见性 / 轴标题 / 数字格式 / 字体，文档里明确写着
   *"Min/max/major_unit/minor_unit and secondary axis are not supported yet"* ——
   轴位置不在其中，所以只能改 pptx 内部。

2. **文件被 editor_sdk 占用时写不进去。** 表现为 `os.replace` 抛
   `PermissionError: [WinError 5] 拒绝访问`。
   `close_file` **不够**（关闭实例后锁仍在），必须关停服务：

   ```bash
   # 关停服务释放句柄（之后 present_files 会按需重启）
   python edsdk.py call shutdown
   ```

   然后才能替换文件。改完重新用预览打开即可。

---

## 与源文件的关系

- `.slide` 是页面源，`slides/` 下的文件与 pptx 里的页面一一对应（01→第 1 页 … 15→第 15 页）。
- 成品经过 `fix_chart_axes.py` 后处理，所以 **pptx 与 `.slide` 存在这一处已知差异**（仅图表轴位）。
- 其他任何内容修改都应以 pptx 为准（本项目约定：pptx 生成后不再回改 DSL 源）。
