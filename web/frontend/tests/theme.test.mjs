import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { runInNewContext } from "node:vm";
import { transformSync } from "esbuild";

test("主题首屏、持久化、动画方向、连点互斥和兼容降级", async () => {
  const boot = readFileSync(new URL("../index.html", import.meta.url), "utf8").match(/<script>([\s\S]*?)<\/script>/)[1];
  const saved = new Map(), attributes = new Map(), animations = [], activeAnimations = new Set();
  const timers = new Map(); let timerId = 0;
  const root = { style: { setProperty(name, value) { this[name] = value; } }, setAttribute: (k, v) => attributes.set(k, v), getAttribute: (k) => attributes.get(k),
    animate: (frames, options) => {
      const animation = { frames, options, cancel() { activeAnimations.delete(animation); } };
      animations.push(animation); activeAnimations.add(animation); return animation;
    } };
  const storage = { getItem: (key) => saved.get(key), setItem: (key, value) => saved.set(key, value) };
  const environment = { document: { documentElement: root }, localStorage: storage,
    matchMedia: () => ({ matches: true }), innerWidth: 1200, innerHeight: 800,
    setTimeout: (callback) => { const id = ++timerId; timers.set(id, callback); return id; },
    clearTimeout: (id) => timers.delete(id) };
  runInNewContext(boot, environment);
  assert.equal(attributes.get("theme-mode"), "dark", "首次访问采用系统配色");
  saved.set("paper2video.theme", "light");
  runInNewContext(boot, environment);
  assert.equal(attributes.get("theme-mode"), "light", "保存的选择优先于系统配色");
  runInNewContext(boot, { ...environment, localStorage: { getItem() { throw Error("禁用存储"); } } });
  assert.equal(attributes.get("theme-mode"), "dark", "禁用存储仍能初始化");

  const original = Object.fromEntries(Object.keys(environment).map((k) => [k, globalThis[k]]));
  Object.assign(globalThis, environment);
  const source = readFileSync(new URL("../src/theme.ts", import.meta.url), "utf8");
  const { code } = transformSync(source, { loader: "ts", format: "esm" });
  const { toggleTheme } = await import(`data:text/javascript;base64,${Buffer.from(code).toString("base64")}`);
  const click = { detail: 1, clientX: 1010, clientY: 60 };
  const changed = [];
  try {
    await toggleTheme(click, (theme) => changed.push(theme));
    assert.equal(root.style.colorScheme, "light", "无 View Transitions API 时仍可切换");
    assert.equal(saved.get("paper2video.theme"), "light");
    let starts = 0, release;
    document.startViewTransition = (commit) => {
      starts++; commit();
      return { ready: Promise.resolve(), finished: new Promise((resolve) => { release = resolve; }), skipTransition() {} };
    };
    globalThis.matchMedia = () => ({ matches: false });
    const first = toggleTheme(click, (theme) => changed.push(theme));
    await new Promise(setImmediate);
    for (let i = 0; i < 5; i++) await toggleTheme(click, (theme) => changed.push(theme));
    assert.equal(starts, 1, "连点不叠加快照");
    assert.equal(animations[0].options.pseudoElement, "::view-transition-old(root)");
    assert.equal(animations[0].options.fill, 'both', '动画前后都保持裁切，防止快照全屏闪出');
    assert.equal(root.style['--theme-x'], '1010px');
    assert.match(animations[0].frames.clipPath[1], /circle\(0px at 1010px 60px\)/);
    const radius = Number(animations[0].frames.clipPath[0].match(/circle\(([\d.]+)px/)[1]);
    assert.ok(radius >= Math.hypot(1010, 740), "裁切半径覆盖最远角");
    release(); await first;
    assert.equal(activeAnimations.size, 0, "快照结束必须取消填充动画，否则下一轮旧页面会继续被裁切为零");
    assert.equal(timers.size, 0, "正常结束清理超时定时器");
    const second = toggleTheme({ ...click, detail: 0, currentTarget: { getBoundingClientRect: () => ({ left: 900, top: 30, width: 80, height: 40 }) } }, () => {});
    await new Promise(setImmediate);
    assert.equal(animations[1].options.pseudoElement, "::view-transition-new(root)");
    assert.equal(animations[1].frames.clipPath[0], "circle(0px at 940px 50px)", "键盘触发从按钮中心开始");
    const css = readFileSync(new URL('../src/styles.css', import.meta.url), 'utf8');
    assert.match(css, /theme-mode="light"\]::view-transition-new\(root\)\s*\{[^}]*clip-path:\s*circle\(0px/, 'ready 之前的新亮色快照必须有初始裁切');
    assert.doesNotMatch(css, /::view-transition-group\(root\)\s*\{[^}]*animation:\s*none/, '保留 group 的快照尺寸动画，避免旧快照消失而露出白底');
    assert.equal(animations[1].options.fill, 'both', '最终圆形覆盖要保留到快照层销毁');
    release(); await second;
    assert.equal(activeAnimations.size, 0, "两个方向都清理动画，不把裁切留给下一轮");
    for (let i = 0; i < 6; i++) {
      const run = toggleTheme(click, () => {}); await new Promise(setImmediate);
      release(); await run; assert.equal(activeAnimations.size, 0, "连续切换不积累伪元素动画");
    }
    globalThis.matchMedia = () => ({ matches: true });
    await toggleTheme(click, () => {});
    assert.equal(starts, 8, "减少动效时直接切换");
    globalThis.matchMedia = () => ({ matches: false });
    document.startViewTransition = () => { throw Error("页面非活跃"); };
    localStorage.setItem = () => { throw Error("禁用存储"); };
    await toggleTheme(click, () => {});
    assert.equal(root.style.colorScheme, "light", "浏览器或存储异常不会阻止切换");
    let skipped = false;
    document.startViewTransition = (commit) => { commit(); return {
      ready: Promise.reject(Error("快照被跳过")), finished: Promise.resolve(), skipTransition: () => { skipped = true; },
    }; };
    await toggleTheme(click, () => {});
    assert.equal(root.style.colorScheme, "dark");
    assert.ok(skipped, "快照失败时清理过渡并保留主题");
    for (const phase of ['ready', 'finished']) {
      let lateCommit, skipCount = 0;
      const before = root.style.colorScheme;
      document.startViewTransition = (commit) => {
        lateCommit = commit;
        if (phase === 'finished') commit();
        return { ready: phase === 'ready' ? new Promise(() => {}) : Promise.resolve(),
          finished: new Promise(() => {}), skipTransition() { skipCount++; } };
      };
      const hanging = toggleTheme(click, () => {});
      await new Promise(setImmediate);
      assert.equal(timers.size, 1);
      [...timers.values()][0](); await hanging;
      assert.equal(skipCount, 1, phase + ' 超时跳过过渡');
      assert.notEqual(root.style.colorScheme, before, '超时仍提交目标主题');
      assert.equal(activeAnimations.size, 0, '超时也销毁填充裁切');
      assert.equal(timers.size, 0);
      globalThis.matchMedia = () => ({ matches: true });
      await toggleTheme(click, () => {});
      assert.equal(root.style.colorScheme, before, '超时后再次点击可以正常切换');
      lateCommit();
      assert.equal(root.style.colorScheme, before, '迟到的旧回调不能覆盖后续主题');
      globalThis.matchMedia = () => ({ matches: false });
    }
  } finally {
    for (const [key, value] of Object.entries(original)) {
      if (value === undefined) delete globalThis[key]; else globalThis[key] = value;
    }
  }
});
