/*!
 * theme-toggle.js —— 从点击位置圆形扩散的亮/暗主题切换
 * 原理：View Transitions API 拍新旧快照 + clip-path circle 从点击位置擦换整页
 * 依赖：无（配合同目录 theme-toggle.css 使用）
 *
 * 用法：
 *   <link rel="stylesheet" href="theme-toggle.css">
 *   <script src="theme-toggle.js"></script>
 *   <button class="theme-toggle" onclick="ThemeToggle.toggle(event)">🌞/🌙</button>
 *
 * API：
 *   ThemeToggle.toggle(event)   在点击位置圆形切换，event 建议传（决定圆心）
 *   ThemeToggle.set('dark'|'light'|'auto')  直接设置，无动画
 *   ThemeToggle.get()           读取当前偏好（含 'auto'）
 *   ThemeToggle.current()       读取实际生效的配色（'light'|'dark'）
 */
(function (global) {
  'use strict';

  var STORAGE_KEY = 'color-scheme-preference'; // 'light' | 'dark' | 'auto'
  var DURATION = 600;
  var EASING = 'ease-in-out';

  var root = document.documentElement;
  var mqlDark = window.matchMedia('(prefers-color-scheme: dark)');
  var mqlReduce = window.matchMedia('(prefers-reduced-motion: reduce)');

  // 动画期间的互斥锁：防止连点导致快照堆叠、白屏
  var switching = false;

  /* ---------------- 持久化 ---------------- */

  function readStored() {
    try {
      return localStorage.getItem(STORAGE_KEY) || 'auto';
    } catch (e) {
      return 'auto';
    }
  }

  function writeStored(pref) {
    try {
      localStorage.setItem(STORAGE_KEY, pref);
    } catch (e) {
      /* 隐私模式等场景写入失败，忽略即可 */
    }
  }

  /* ---------------- 主题解析与绘制 ---------------- */

  // 'auto' 跟随系统，其余原样
  function resolve(pref) {
    if (pref === 'auto') return mqlDark.matches ? 'dark' : 'light';
    return pref === 'dark' ? 'dark' : 'light';
  }

  // 只改 DOM，不做动画 —— 同时作为 startViewTransition 的回调
  function paint(scheme) {
    root.classList.toggle('dark', scheme === 'dark'); // 供 CSS 选择器与 .dark::view-transition-old 使用
    root.setAttribute('theme-mode', scheme);          // TDesign 等库约定的属性
    root.dataset.theme = scheme;                      // 通用 data 属性，方便自定义 CSS
    root.style.colorScheme = scheme;                  // 让滚动条/表单控件/输入框跟着变
  }

  function currentScheme() {
    return root.classList.contains('dark') ? 'dark' : 'light';
  }

  /* ---------------- 核心：圆形扩散切换 ---------------- */

  async function toggle(event) {
    if (switching) return; // 同一入口互斥，动画中不创建第二套快照。
    var nextScheme = currentScheme() === 'dark' ? 'light' : 'dark';
    var committed = false;
    var setTheme = function () {
      if (committed) return; // 超时后的迟到回调不能覆盖下一轮主题。
      committed = true;
      paint(nextScheme);
      writeStored(nextScheme);
    };
    if (typeof document.startViewTransition !== 'function' || mqlReduce.matches) {
      setTheme();
      return;
    }

    // 键盘触发用按钮中心；在 await 前读取事件，避免 currentTarget 被框架清空。
    var rect = event && event.currentTarget && event.currentTarget.getBoundingClientRect();
    var pointer = event && event.detail > 0;
    var x = pointer ? event.clientX : rect ? rect.left + rect.width / 2 : window.innerWidth - 48;
    var y = pointer ? event.clientY : rect ? rect.top + rect.height / 2 : 48;
    var radius = Math.hypot(Math.max(x, window.innerWidth - x), Math.max(y, window.innerHeight - y));
    var clipPath = ['circle(0px at ' + x + 'px ' + y + 'px)',
      'circle(' + radius + 'px at ' + x + 'px ' + y + 'px)'];
    root.style.setProperty('--theme-x', x + 'px');
    root.style.setProperty('--theme-y', y + 'px');
    root.dataset.themeTransition = nextScheme;
    switching = true;
    var transition, animation, timer;
    // 示例为 600ms 动画预留 2s 总等待；接入异步主题源时按实际延迟调整。
    var timeout = new Promise(function (_, reject) {
      timer = setTimeout(function () { reject(new Error('Theme transition timed out')); }, 2000);
    });
    try {
      transition = document.startViewTransition(setTheme);
      void transition.finished.catch(function () {});
      await Promise.race([transition.ready, timeout]);
      animation = root.animate({ clipPath: nextScheme === 'dark' ? clipPath.slice().reverse() : clipPath }, {
        duration: DURATION, easing: EASING, fill: 'both',
        pseudoElement: nextScheme === 'dark' ? '::view-transition-old(root)' : '::view-transition-new(root)'
      });
      await Promise.race([transition.finished, timeout]);
    } catch (e) {
      if (transition) transition.skipTransition();
      setTheme();
    } finally {
      clearTimeout(timer);
      delete root.dataset.themeTransition;
      // 保留末帧直到快照退出，再取消；避免填充裁切遗留到同名伪元素的下一轮。
      if (animation) animation.cancel();
      switching = false;
    }
  }

  /* ---------------- 直接设置（无动画） ---------------- */

  function setPref(pref) {
    var scheme = resolve(pref);
    paint(scheme);
    writeStored(pref);
    return scheme;
  }

  /* ---------------- 初始化 ---------------- */

  function bindSystemListener() {
    var handler = function () {
      // 仅当用户处于「跟随系统」时才响应系统切换
      if (readStored() === 'auto') paint(resolve('auto'));
    };
    if (typeof mqlDark.addEventListener === 'function') {
      mqlDark.addEventListener('change', handler);
    } else if (typeof mqlDark.addListener === 'function') {
      mqlDark.addListener(handler);
    }
  }

  function init() {
    paint(resolve(readStored()));
    bindSystemListener();
    return currentScheme();
  }

  // 立刻执行：documentElement 在脚本执行时一定存在，
  // 这样即使在 <head> 中引入也能尽早打上主题，减少首屏亮暗闪烁。
  init();

  global.ThemeToggle = {
    init: init,
    toggle: toggle,
    set: setPref,
    get: readStored,
    current: currentScheme,
    DURATION: DURATION
  };
})(typeof window !== 'undefined' ? window : this);
