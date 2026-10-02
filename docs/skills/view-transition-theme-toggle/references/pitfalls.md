# 排查表：现象 → 根因 → 修法

按「症状」检索。列出的是诊断候选，必须结合代码与真实渲染证据确认；不要把一个项目的现象自动视为所有浏览器的同一根因。

---

## 1. 完全没有动画，主题是「啪」一下变的

**根因排查顺序（自外向内）：**

1. `document.startViewTransition` 是 `undefined`（浏览器不支持）。
   → 控制台执行 `typeof document.startViewTransition` 确认。以运行时特性检测为准，不把固定版本表当作当前兼容性保证。
   不支持时**只能降级**，不要试图用 JS 动画模拟整页快照（成本高、效果差）。
2. 用了 `document.startViewTransition(setTheme)` 但漏了 `.ready.then(...)` 里的 `animate`。
   → 没有 animate 就只有默认 cross-fade，观感近似「渐变一下」。
3. 动画被 `prefers-reduced-motion: reduce` 短路了。
   → 在 DevTools Rendering 面板检查 "Emulate CSS prefers-reduced-motion" 是否为 reduce。

---

## 2. 有过渡，但看不出「圆」，只是一片糊

**根因**：没有重置伪元素的默认动画。

```css
::view-transition-old(root),
::view-transition-new(root) {
  animation: none;
  mix-blend-mode: normal;
}
```

`mix-blend-mode` 尤其容易漏 —— 默认的交叉淡入会让两个快照叠加发灰，圆边缘完全糊掉。

---

## 3. 变暗有效果，变亮没有（或反过来的镜像情况）

**根因**：`new` 层默认 `z-index` 高于 `old` 层。博客的变暗实现动画旧亮色快照，
让它从整屏圆形收缩，露出新暗色快照；如果 `old` 在下面，这个裁切会被盖住。

```css
.dark::view-transition-old(root) {
  z-index: 9999;
}
```

**常见错误写法**：写成裸的 `::view-transition-old(root)`。这样在变暗时也会生效，
于是两个方向都可能出现错误叠层，容易误判为「API 有问题」。

必须在选择器里带上「当前是深色」这一状态前缀。

---

## 4. 两个方向的方向感都反了

**根因**：`clipPath` 数组与 `pseudoElement` 的配对写反了。正确的配对是固定的：

| 目标 | clipPath | pseudoElement |
| --- | --- | --- |
| 变暗（目标是 dark） | `[radius, 0px]`，旧亮色快照收缩 | `::view-transition-old(root)` |
| 变亮（目标是 light） | `[0px, radius]`，新亮色快照扩展 | `::view-transition-new(root)` |

这组映射来自博客示例。判断动画时先确认 old/new 各自承载哪种主题，再同时核对裁切半径顺序、`pseudoElement` 和层级；不要只凭“目标主题在 new”来推断动画层。

---

## 5. 圆盖不满屏幕，切完还剩一块没变

**根因**：半径算成了「到屏幕边缘的距离」而不是「到最远角的距离」。

```ts
// 正确：x/y 各取向外的较大值，再用 Math.hypot 求斜边
const radius = Math.hypot(
  Math.max(x, window.innerWidth - x),
  Math.max(y, window.innerHeight - y)
);
```

**连带问题**：窗口尺寸变化（如移动端旋转屏幕）后如果复用缓存的 radius，也会盖不满。
半径必须在**每次切换时重新计算**，不要缓存。

---

## 6. 圆从屏幕左上角冒出来，不是从按钮位置

**根因**：`pos` 还是初始值 `{ x: 0, y: 0 }`。两种典型成因：

- **键盘触发**：用 Enter/Space 激活按钮时，`click` 事件的 `clientX/clientY` 是 0。
  → 用 event.detail === 0 判定，优先使用按钮矩形中心；没有目标元素时才回退到视口合理位置。
- **Vue 的 `el-switch`**：`@change` 的触发时机晚于 `click`，若直接监听 `@click`（冒泡阶段），
  `onChange` 读到的可能是上一次的位置。
  → 必须用 `@click.capture`，在捕获阶段先记录坐标。

---

## 7. 刷新页面后先闪一下亮色（或暗色）再切过来

**根因**：首屏 FOUC。主题类名由 JS 在脚本执行时才写上，而脚本通常在 `<body>` 末尾，
浏览器已经完成首次绘制。

**修法**：在 `<head>` 里、**任何样式表之前**内联一段同步脚本，提前打上类名：

```html
<script>
  (function () {
    try {
      var saved = localStorage.getItem('color-scheme-preference');
      var dark = saved === 'dark' ||
        ((!saved || saved === 'auto') && matchMedia('(prefers-color-scheme: dark)').matches);
      if (dark) document.documentElement.classList.add('dark');
      document.documentElement.style.colorScheme = dark ? 'dark' : 'light';
    } catch (e) {}
  })();
</script>
```

SSR 场景同理：把这段放进 `<head>` 模板（内联、无 async/defer），或由服务端根据 cookie 直接渲染
`<html class="dark">` —— 后者无闪烁且无水合不匹配问题。

---

## 8. TypeScript 打包报「Document 上不存在属性 startViewTransition」

**根因**：TS 的 `lib.dom.d.ts` 版本较老，尚未收录该 API。

**修法**：项目内补一份声明（可选属性，正好与运行时降级判断对齐）：

```ts
interface ViewTransition {
  ready: Promise<void>;
  finished: Promise<void>;
  updateCallbackDone: Promise<void>;
  skipTransition(): void;
}
interface Document {
  startViewTransition?: (cb: () => void | Promise<void>) => ViewTransition;
}
```

因为声明成了可选，`document.startViewTransition(...)` 会提示可能为 undefined ——
这正好强制你写降级分支，是好事，不要在调用处加 `!` 硬断言。

---

## 9. 快速连点后白屏 / 快照错位

**候选原因**：上一次 transition 尚未 finished 又启动新的；或 fill 动画残留；或 ready/finished 未正常收尾导致锁一直不释放。

**修法**（任选）：

- 同一入口互斥，保存 Animation 并在本轮 finished 后 cancel；ready/finished 共用有限等待，异常时 skipTransition + finally 清理（assets/theme-toggle.js 的默认方案）；
- 若用户明确要求中途改方向，可主动中止前一轮；必须同时清理该轮 Animation 与锁，并处理迟到提交，不能只调用 skipTransition 就叠加下一轮。

---

## 10. 切换瞬间页面横向抖动一下

**根因**：亮色主题出现滚动条、暗色主题没有（或反之），视口宽度变化导致重排。

**修法**：在 `html` 上恒占滚动条宽度：

```css
html { scrollbar-gutter: stable; }
```

---

## 11. 切完主题，滚动条和输入框还是老配色

**根因**：只改了 class，没改 CSS 的 `color-scheme` 属性。该属性控制浏览器**原生控件**
（滚动条、`<input>`、`<select>`、日期选择器、自动填充背景）的渲染配色。

**修法**：`document.documentElement.style.colorScheme = 'dark' | 'light'`。
或在 CSS 里 `html.dark { color-scheme: dark; }`。

---

## 12. 页面里有组件在切换时错位 / 闪烁

**根因**：你的页面或某个 UI 库也用了 `view-transition-name`，多个快照组之间互相干扰。

**修法**：切换主题时只保留 root 快照 —— 给可能冲突的元素显式关闭：

```css
.some-widget { view-transition-name: none; }
```

---

## 13. 控制台报 `document is not defined`

**根因**：代码在 SSR / SSG（Next.js、Nuxt 的服务端渲染阶段）被执行。

**修法**：所有涉及 `document` 的逻辑放进 `useEffect` / `onMounted` / `if (typeof window !== 'undefined')`；
或把主题切换组件标为客户端专用（`'use client'` / `<ClientOnly>`）。

---

## 14. 暗→亮时未扩散区域变纯色，或第二轮才出现空白

优先区分：首轮就坏，还是至少往返一次后才坏。

- 保存 root.animate() 返回对象，检查 fill: both/forwards 的裁切是否在上一轮结束后仍生效。旧快照可能继承已收缩到 0 的裁切；正常 finished 后 cancel，异常路径也清理。不要为压住空白直接删 fill，这可能重新引起闪屏。
- 检查 old/new 的层级和 opacity，确认旧快照确实可见；检查 group(root) 快照尺寸与其他全局样式。不要仅凭空白截图断定必须禁用或恢复 group 动画。
- 只重置 old/new 的交叉淡化，保留 group 几何动画为默认起点。若尺寸或字体/布局在切换中改变，先解决重排来源。
- 用中间帧确认：圆外是旧页面内容，而不是根背景色。终态截图无法验证。

## 15. 亮色一瞬间铺满页面，然后才开始扩散

新快照已经显示，而 ready 回调中的 WAAPI 裁切尚未应用。在 startViewTransition 前保存 --theme-x/--theme-y，并只对 data-theme-transition="light" 的 new(root) 设置 circle(0px ...)；fill: both 保持首末帧直至快照退出，finally 删除临时属性并 cancel 动画。CSS 与 JS 缺一不可，不能把零半径规则永久附加到所有亮色页面转场。

## 16. 连点后按钮一直禁用，或卡在半幅页面

只等 finished 解锁不够。模拟 ready 拒绝及 ready/finished 不返回，检查有限等待、skipTransition、Animation.cancel、定时器和锁清理。超时值应覆盖正常渲染和动画延迟；示例的 2 秒可调整，不能宣称浏览器必定在这个时间恢复。

超时兜底与浏览器更新回调均可能提交主题，提交必须幂等；旧回调迟到时不得覆盖后续主题。框架 busy 状态也应在 finally 恢复。若 apply 本身出错，应交给调用方报告，不能把失败当作切换成功。

## 自查速查（30 秒定位）

1. 控制台 `typeof document.startViewTransition` → 不支持就不必往下查。
2. 切主题瞬间在 DevTools Elements 里找 `::view-transition-old(root)` / `-new(root)` 节点
   → **存在**说明 API 生效，问题在 CSS（看第 2、3 条）；
   **不存在**说明 JS 逻辑没走到（看第 1、4 条）。
3. 检查 `html` 上的 `dark` class 与 `color-scheme` 是否同步更新
   → 未同步说明是状态管理/持久化问题，与动画无关。
4. 在 DevTools Rendering 里把 prefers-reduced-motion 设为 reduce 再试
   → 行为正常说明只是动画被降级，不是 bug。
