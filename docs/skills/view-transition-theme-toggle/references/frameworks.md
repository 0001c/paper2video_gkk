# Vue 3、React 与原生接入

先将 [lifecycle.md](lifecycle.md) 的公共核心保存为 theme-transition.ts，配套接入 assets/theme-toggle.css 的快照规则；选择器改为项目实际主题状态。以下只展示各框架如何提交状态，超时、裁切清理与互斥由公共核心处理，不另写 ready.then(animate) 的简化副本。

## React

主题 DOM、按钮文字和图标在同一个快照回调内同步提交，不依赖 useEffect 上色。首屏由 head 内联初始化脚本决定；读取项目实际根属性，存储键沿用项目已有值。

```tsx
import { useRef, useState } from 'react';
import { flushSync } from 'react-dom';
import { runThemeTransition, type Theme } from './theme-transition';

export function ThemeButton() {
  const [theme, setTheme] = useState<Theme>(() => document.documentElement.getAttribute('theme-mode') === 'dark' ? 'dark' : 'light');
  const [busy, setBusy] = useState(false);
  const guard = useRef(false);
  return <button disabled={busy} aria-label={theme === 'dark' ? '切换到明亮模式' : '切换到暗色模式'}
    onClick={async (event) => {
      if (guard.current) return;
      guard.current = true; setBusy(true);
      const next: Theme = document.documentElement.getAttribute('theme-mode') === 'dark' ? 'light' : 'dark';
      try {
        await runThemeTransition(event, next, () => {
          const root = document.documentElement;
          root.classList.toggle('dark', next === 'dark');
          root.setAttribute('theme-mode', next);
          root.style.colorScheme = next;
          try { localStorage.setItem('color-scheme-preference', next); } catch {}
          flushSync(() => setTheme(next));
        });
      } finally { guard.current = false; setBusy(false); }
    }}>
    {theme === 'dark' ? '明亮' : '暗色'}
  </button>;
}
```

如项目已有 store，apply 应调用它并保证本次同步渲染；不要让示例 useState 与 store 同时控制同一主题。

## Vue 3

Vue 状态变更后返回 nextTick，让快照等待本次渲染。以下假定 head 已用同一存储键初始化根属性：

```vue
<template>
  <button :disabled="busy" :aria-label="theme === 'dark' ? '切换到明亮模式' : '切换到暗色模式'" @click="toggle">
    {{ theme === 'dark' ? '明亮' : '暗色' }}
  </button>
</template>

<script setup lang="ts">
import { ref, nextTick } from 'vue';
import { runThemeTransition, type Theme } from './theme-transition';
const theme = ref<Theme>(document.documentElement.getAttribute('theme-mode') === 'dark' ? 'dark' : 'light');
const busy = ref(false);
async function toggle(event: MouseEvent) {
  if (busy.value) return;
  busy.value = true;
  const next: Theme = theme.value === 'dark' ? 'light' : 'dark';
  try {
    await runThemeTransition(event, next, async () => {
      theme.value = next;
      const root = document.documentElement;
      root.classList.toggle('dark', next === 'dark');
      root.setAttribute('theme-mode', next);
      root.style.colorScheme = next;
      try { localStorage.setItem('color-scheme-preference', next); } catch {}
      await nextTick();
    });
  } finally { busy.value = false; }
}
</script>
```

使用 Element Plus el-switch 时，以 :model-value 读取状态，在 @change 中通过公共入口提交；用 @click.capture 保存本次 detail/clientX/clientY 与按钮矩形。键盘触发 detail=0 时使用按钮中心，不能复用上一轮鼠标位置。不要让 v-model、VueUse 的自动主题绘制与手动根属性写入同时各自提交。如果已有 useColorMode，就把原有 mode 更新及 nextTick 放入 apply，移除示例中的第二份状态来源。

## 原生 JavaScript

无框架页面直接引入 assets/theme-toggle.js + assets/theme-toggle.css，调用 ThemeToggle.toggle(event)。API 缺失、减少动效和存储失败均支持降级；600ms 动画示例设置 2 秒等待兜底，ready/finished 超时会清理并解锁。

head 初始化脚本与外部 JS 的存储键和主题属性必须一致。直接 ThemeToggle.set() 或系统跟随更新不要与未完成的主题快照并行；有多个入口时统一协调，不能只有按钮一处上锁。

## TypeScript 类型

优先使用项目现有 lib.dom 类型。只有确实缺失时才补 Document.startViewTransition、ViewTransition 的 ready/finished/updateCallbackDone/skipTransition 声明；避免与现代 DOM 声明重复冲突。运行时仍检测 typeof document.startViewTransition，不用非空断言代替降级。
