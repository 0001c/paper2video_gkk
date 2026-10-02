# 快照生命周期与可复制核心

## 顺序与清理

旧主题 → 捕获 old → 一次提交主题 → 捕获 new → ready → 裁切动画 → finished → cancel Animation → 解锁。

初始裁切只保护新亮色快照在 ready 前的窗口；fill: both 保护动画首尾；cancel 结束这一轮效果。三个措施需要配套，不能只增加永久零半径 CSS 或只删 fill 来压住闪屏。

正常路径必须在 transition.finished 后取消动画。异常路径先 skipTransition，保留已经选择的目标主题，再清理。超时并不保证浏览器已经执行更新回调，所以提交应缓存一次结果；后到的回调也只能返回同一个提交结果，不能重写下一轮状态。

以下为公共模块 theme-transition.ts；用项目现有的主题入口提供 apply，不引入第二份主题状态。模块实例应共享，其他直接设置主题的入口应与其协调，避免动画中发生相反的主题写入。不要同时加载原生全局 ThemeToggle 与另一个框架状态控制器。apply 应先同步写入主题，Promise 仅用于等待本地渲染（如 nextTick）；不要在其中获取远程数据或长期等待。若 apply 自身可能挂起，其取消/超时由主题源负责，本示例的浏览器等待兜底无法中止任意异步业务。


```ts
export type Theme = 'light' | 'dark';
type Activation = Pick<MouseEvent, 'detail' | 'clientX' | 'clientY'> & {
  currentTarget?: EventTarget | null;
};
let switching = false; // 放在共享模块，不能每次调用重新创建。

export async function runThemeTransition(event: Activation, next: Theme, apply: () => void | Promise<void>) {
  if (switching) return;
  const root = document.documentElement;
  const rect = event.currentTarget instanceof Element ? event.currentTarget.getBoundingClientRect() : undefined;
  const x = event.detail > 0 ? event.clientX : rect ? rect.left + rect.width / 2 : innerWidth - 48;
  const y = event.detail > 0 ? event.clientY : rect ? rect.top + rect.height / 2 : 48;
  let committed: Promise<void> | undefined;
  // 缓存一次提交及其 Promise，浏览器迟到回调不会重复 apply。
  const commit = () => committed ??= Promise.resolve().then(apply);
  let transition: ViewTransition | undefined;
  let animation: Animation | undefined;
  let timer: ReturnType<typeof setTimeout> | undefined;
  switching = true;
  try {
    if (typeof document.startViewTransition !== 'function' || matchMedia('(prefers-reduced-motion: reduce)').matches) {
      await commit(); return;
    }
    const radius = Math.hypot(Math.max(x, innerWidth - x), Math.max(y, innerHeight - y));
    const clips = [`circle(0px at ${x}px ${y}px)`, `circle(${radius}px at ${x}px ${y}px)`];
    root.style.setProperty('--theme-x', x + 'px');
    root.style.setProperty('--theme-y', y + 'px');
    root.dataset.themeTransition = next;
    const timeout = new Promise<never>((_, reject) => {
      timer = setTimeout(() => reject(new Error('Theme transition timed out')), 2000);
    });
    transition = document.startViewTransition(commit);
    void transition.finished.catch(() => {});
    await Promise.race([transition.ready, timeout]);
    animation = root.animate({ clipPath: next === 'dark' ? [...clips].reverse() : clips }, {
      duration: 600, easing: 'ease-in-out', fill: 'both',
      pseudoElement: next === 'dark' ? '::view-transition-old(root)' : '::view-transition-new(root)',
    });
    await Promise.race([transition.finished, timeout]);
  } catch {
    transition?.skipTransition();
    await commit(); // apply 本身失败会向调用方抛出，不假装主题提交成功。
  } finally {
    clearTimeout(timer);
    delete root.dataset.themeTransition;
    animation?.cancel();
    switching = false;
  }
}
```

CSS 使用 assets/theme-toggle.css 的前三段，或者将 .dark 选择器换为项目实际属性（例如 [theme-mode="dark"]）。保留 data-theme-transition 的短期作用域与 --theme-x/--theme-y。这些属性/变量必须由 JS 与 CSS 同时接入。

## 2026-10-02 实际问题与证据范围

在一个长表单工作台中，用户报告暗→亮时圆外变为无内容纯色，连续切换偶尔卡在半途。代码检查发现 fill: both 的 Animation 没有显式清理，且锁只依赖 finished，没有异常超时兜底。修复加入 cancel、2 秒兜底和幂等提交，回归测试覆盖相应时序，实际浏览器明→暗→明终态与按钮解锁正常。

将填充动画残留作为优先检查项，但不要声称所有浏览器的纯色空白都由它造成；还应检查 old/new 层级、opacity、group 的尺寸动画，以及主题更新是否进入快照。该次工具每步延迟约 20–30 秒，未可靠捕获 600ms 中间帧，因此不能把终态截图写成逐帧证据。

## 验证分层

| 检查 | 能证明什么 | 不能证明什么 |
| --- | --- | --- |
| Node 替身回归 | 清理、互斥、超时、迟到回调、降级的代码分支 | 浏览器伪元素的真实合成与视觉 |
| 完整往返后的截图 | 终态主题、内容、按钮和页面位置 | 圆外内容在扩散中保留、首帧不闪、快速连点 |
| 录像/受支持工具捕获的中间帧 | 裁切边界、旧内容保留与实际视觉时序 | 未运行的其他浏览器行为 |

用 DevTools 的动画面板或受支持的录像/帧捕获工具观察至少一组暗→亮与连续往返。结束后检查 root 的 getAnimations() 中没有本技能留下的已结束填充裁切动画（过滤目标伪元素，不能要求整页所有动画为零）。如果操作工具本身超时，应先读取最新状态，不能直接重复点击、绕过安全设置或切换到未授权的自动化渠道。
