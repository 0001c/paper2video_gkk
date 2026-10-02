---
name: view-transition-theme-toggle
description: 用 View Transitions API 和 clip-path circle 实现点击位置为圆心的整页明暗圆形擦换，或排查方向、层级、闪屏、内容空白和连点卡住问题。覆盖 Vue 3、React 与原生 JavaScript；不用于普通颜色渐变或按钮涟漪。
metadata:
  short-description: "整页明暗圆形擦换、快照生命周期与异常降级"
  agent_created: true
---

# 点击起点圆形擦换主题

浏览器拍摄旧/新页面快照，clip-path: circle() 裁切其中一层。圆心跟随本次点击，半径覆盖视口最远角。复用项目已有主题变量、存储键和组件库，不额外创建一套主题状态。

## 选择正确入口

- 改造已有项目：先读 [references/adoption.md](references/adoption.md)，找到唯一主题提交入口及所有调用方。
- 出现闪屏、方向错误、纯色空白或卡住：读 [references/pitfalls.md](references/pitfalls.md)。
- 接入框架：读 [references/frameworks.md](references/frameworks.md) 与 [references/lifecycle.md](references/lifecycle.md)。框架示例共用一套生命周期逻辑。
- 原生页面：使用 [assets/theme-toggle.js](assets/theme-toggle.js) 和 [assets/theme-toggle.css](assets/theme-toggle.css)，[assets/demo.html](assets/demo.html) 可直接演示。

## 快照与方向

本技能默认保留博客示例的层映射；用户明确要求两个方向均向外扩散时，按目标视觉重新选择动画层与层级。

| 目标主题 | 上层快照 | 圆形裁切 | 下层快照 |
| --- | --- | --- | --- |
| dark | old：旧亮色 | 最远半径 → 0，圆外逐渐露出暗色 | new：新暗色 |
| light | new：新亮色 | 0 → 最远半径，圆内逐渐露出亮色 | old：旧暗色 |

两种方向不是相同的向外扩散。不要仅凭“目标主题在 new”推断动画层。未被擦换的区域应保留旧页面内容，而非裸主题底色。只复现圆形擦换，不额外添加波纹或全屏发光。

点击坐标在第一次 await 前取出；键盘激活优先用按钮中心，无目标元素时再回退到视口合理位置。半径每轮按当前视口重新计算：

```ts
const radius = Math.hypot(Math.max(x, innerWidth - x), Math.max(y, innerHeight - y));
```

## 必须配套的生命周期

1. 在快照回调中完成主题 DOM 与框架渲染。React 用 flushSync；Vue 改状态后等待 nextTick。不要靠稍后执行的 effect 才上色。
2. 调用 startViewTransition 前保存圆心 CSS 变量，并只为本轮亮色新快照设零半径初始裁切，防止 ready 前闪出整屏。属性在 finally 清理，避免影响其他页面转场。
3. 重置 old/new 的默认淡化与混合，设置符合当前主题的层级。不要一并普遍禁用 group(root) 的几何动画；遇到空白先检查快照尺寸、填充动画残留及其他样式。
4. 保存 root.animate() 返回的 Animation，使用 fill: both 将裁切保留到快照退出；正常 finished 后显式 cancel()，异常清理时也取消。不能只去掉 fill，否则可能重新出现首帧/末帧闪屏。
5. 同一入口互斥，正常等待 transition.finished。ready 和 finished 都可能失败或迟迟不返回：设置有限等待，异常时 skipTransition、保留目标主题，并在 finally 清定时器、动画和锁。示例 600ms 动画使用 2s 总等待；这是可调整的示例值，不是平台保证。
6. 主题提交必须幂等；超时处理与迟到回调不能重复提交、覆盖后续主题。立即处理 finished 的拒绝，避免等待 ready 时产生未处理异常。
7. 不支持 API 或用户要求减少动效时，直接切主题。存储不可用时仍能切换，color-scheme 与主题保持一致。

可复制的公共核心见 [references/lifecycle.md](references/lifecycle.md)。不要复制只有 ready.then(animate) 的概念片段作为生产实现。

## 验证与交付

- 明→暗→明至少完整往返三轮；动画中快速连点至少五次，检查互斥、页面内容和按钮解锁。
- 亮色扩散尚未覆盖的区域保留旧暗色页面；开头不闪整屏，结尾不回弹。用录像或中间帧观察，静态结束截图不能证明这些性质。
- 检查每轮结束无本技能遗留的填充动画；模拟 ready 拒绝、ready/finished 永不响应、animate 抛错和迟到提交，确认清理及再次切换。
- 验证键盘中心、最大角半径、减少动效、无 API、存储禁用、刷新首屏及有滚动条的长页面。
- 示例逻辑可运行 node scripts/smoke_theme_toggle.cjs；结构可用 skill-creator 的 quick_validate.py 校验。逻辑测试不替代真实渲染。
- 浏览器工具超时后先观察新状态，动作可能已经执行。操作延迟大于动画时长时，不能宣称完成快速连点或逐帧视觉验证；分别报告代码测试、页面终态、未验证的中间画面，不盲目重复操作或绕过权限。

## 不扩大任务

本技能只处理主题擦换，不自动重设计页面、修改浏览器/系统权限、安装浏览器扩展或更换托管方式。局域网服务不可达先查服务与连接，不把连接失败归因于主题动画。
