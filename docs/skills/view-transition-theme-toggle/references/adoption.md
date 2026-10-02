# 接入现有项目：先分类，再动手

本文件解决的是「我已经有一个网站了，怎么改」。从零新建请直接看 `assets/demo.html`。

---

## 第 0 步：先判断你属于哪一类（别急着抄代码）

三个问题，答案决定改动量：

1. **技术栈是什么** → 决定抄哪份代码（`references/frameworks.md` 里有 Vue / React / 原生三份）
2. **现在有没有亮暗切换** → 决定是「叠加动画」还是「先补主题基建」
3. **主题状态挂在哪个属性上** → 决定 CSS 选择器怎么写（这一步最容易出错）

### 怎么快速判断第 3 点

打开 DevTools → Elements，切一次主题，看 `<html>` 标签上的变化：

| 你看到的 | 对应的 CSS 选择器 |
| --- | --- |
| `<html class="dark">` | `.dark::view-transition-old(root)` |
| `<html data-theme="dark">` | `[data-theme='dark']::view-transition-old(root)` |
| `<html theme-mode="dark">`（TDesign） | `[theme-mode='dark']::view-transition-old(root)` |
| 什么都没有，只是 CSS 变量变了 | 需要先找到「当前处于深色」这个状态的可判定钩子 |

也可以在项目里搜一遍现状：

```bash
# 有没有主题相关逻辑
grep -rn "prefers-color-scheme\|color-scheme\|theme-mode\|classList.*dark\|data-theme" src/
# 有没有 CSS 变量层
grep -rn -- "--bg\|--background\|--foreground" src/ --include=*.css --include=*.scss
# 有没有用 UI 库的主题能力
grep -rn "useColorMode\|useDark\|themeMode\|ConfigProvider" src/
```

---

## 三种现状，对应三种工作量

| 现状 | 要做的事 | 实际改动量 |
| --- | --- | --- |
| **A. 已有亮暗切换** | 只加动画层 | 改 1 个函数 + 加 2 段 CSS |
| **B. 有主题变量，但没有切换入口** | 加一个切换按钮 + 动画 | 新增 1 个组件 + 2 段 CSS |
| **C. 完全没有主题体系** | 先建 CSS 变量层与持久化，再叠动画 | 需要动样式层，工作量最大 |

**判断依据**：能不能在页面上找到一个能切主题的按钮/开关？能 → A；不能但样式里有 `prefers-color-scheme` 或主题变量 → B；样式全是硬编码颜色 → C。

---

## 情况 A：已有亮暗切换（最常见，也是收益最大的）

### 前提：把「改主题」收敛成一个函数

这是**整个改造中最关键的一步**，也是大多数改造失败的原因。

`startViewTransition` 的回调里必须完成**全部** DOM 变更 —— 快照是在回调执行完的瞬间拍的。
如果你的主题状态散落在 store、多个 `useEffect`、或者 UI 库内部，回调执行完时 DOM 还没变，
拍到的快照就是旧的，表现为「动画方向反了」或「什么都没发生」。

所以先做一次收敛：

```ts
// 改造前：主题变更散落各处
setDark(true);
document.documentElement.classList.add('dark');
localStorage.setItem('theme', 'dark');

// 改造后：单一入口
function applyTheme(dark: boolean) {
  document.documentElement.classList.toggle('dark', dark);
  document.documentElement.style.colorScheme = dark ? 'dark' : 'light';
  localStorage.setItem('theme', dark ? 'dark' : 'light');
}
// 其它地方一律调 applyTheme()
```

> React 场景特别提醒：如果 `applyTheme` 里是 `setState`，DOM 变更可能不在回调同步阶段完成。
> 此时用 `flushSync(() => setDark(true))` 强制同步提交，或直接把 DOM 操作写在回调里、状态更新放到
> `transition.finished` 之后。

### 然后接入快照生命周期

1. **记录坐标**：给触发按钮绑 click，存下 `clientX / clientY`。
   Vue 的 `el-switch` 必须用 `@click.capture`，否则 `@change` 里读到的是上一次的位置。
2. **包动画**：使用 [lifecycle.md](lifecycle.md) 的公共核心，在 ready 后裁切；保持 fill: both 到 finished，再 cancel。准备和收尾共用有限等待，异常时跳过、清理和解锁，主题提交幂等。
3. **配套 CSS**：old/new 淡化重置、按主题的层级、只作用于本轮新亮色快照的初始零半径裁切；保存圆心变量并在 finally 清理临时属性。选择器按第 0 步的实际主题属性改写，不普遍禁用 group 几何动画。

完整代码见 `references/frameworks.md`，可直接对照你项目的写法改造。

---

## 情况 B：有主题变量，但没有切换入口

在 A 的基础上，先补一个切换入口：

1. 建一个 `useTheme` 之类的状态（三态：`light` / `dark` / `auto`），持久化到 `localStorage`；
2. `auto` 用 `matchMedia('(prefers-color-scheme: dark)')` 解析，并监听其 `change`；
3. 把解析结果写到 `<html>` 的属性上（统一钩子）；
4. 再按 A 的流程加动画。

`assets/theme-toggle.js` 已经完整实现了 1–3 步（含 `auto` 与系统监听），
无框架项目可以直接引入；有框架的项目建议把这套逻辑翻译成你自己的 hook，避免两套状态并存。

---

## 情况 C：完全没有主题体系

这一步的本质是**样式重构**，不是动画问题，先别碰 View Transitions。

1. 把所有硬编码颜色抽成 CSS 变量（`:root` 一套，深色选择器下一套）；
2. 加 `color-scheme` 属性；
3. 加 `<head>` 内联防闪烁脚本（见 `references/pitfalls.md` 第 7 条）；
4. 确认切换本身能正常工作后，再回来叠加圆形扩散动画。

顺序反了会陷入「动画调不出来，其实是主题根本没切成功」的困境。

---

## 改造完后验证

- [ ] **变暗**：按所选方向擦换；博客代码让旧亮色 `old(root)` 从大圆收缩到 0，并用 `.dark::view-transition-old(root)` 调整层级。
- [ ] **变亮**：视觉上亮色从点击位置向外覆盖；博客代码让新亮色 `new(root)` 从 0 扩展到最远角。
- [ ] **降级**：在 Firefox 或 `Emulate CSS prefers-reduced-motion: reduce` 下，主题仍能切换（无动画）。
- [ ] **刷新**：首屏不闪（先出现亮色/暗色再跳变）。
- [ ] **连点与重复往返**：快速点 5 次并完整往返三轮，不叠加快照、不积累填充裁切、按钮恢复可用。
- [ ] **失败路径**：模拟 ready 拒绝、ready/finished 不返回、animate 抛错与迟到回调，确认目标主题、清理与再次切换。
- [ ] **证据边界**：实际中间帧验证首帧与圆外内容；静态截图和替身测试仅报告各自能证明的范围。

任一条不通过，去 `references/pitfalls.md` 按症状查根因。

---

## 三个「不要做」

1. **不要**用 JS 手写整页快照去模拟这个效果 —— 性能差且复杂度高，浏览器原生能力已经够用。
2. **不要**给 `body` 或 `*` 加 `transition: all`（或 `transition: background-color`）——
   它会和 View Transitions 抢戏，导致动画期间颜色提前变完，圆还没扩散。
3. **不要**在动画进行中触发路由跳转或大范围重排 —— 600ms 的快照冻结窗口内做这些会明显卡顿。
