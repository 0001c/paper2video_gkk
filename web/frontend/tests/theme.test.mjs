import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { runInNewContext } from "node:vm";
import { transformSync } from "esbuild";

test("主题首屏、持久化、动画方向、连点互斥和兼容降级", async () => {
  const boot = readFileSync(new URL("../index.html", import.meta.url), "utf8").match(/<script>([\s\S]*?)<\/script>/)[1];
  const saved = new Map(), attributes = new Map(), animations = [];
  const root = { style: { setProperty(name, value) { this[name] = value; } }, setAttribute: (k, v) => attributes.set(k, v), getAttribute: (k) => attributes.get(k),
    animate: (frames, options) => animations.push({ frames, options }) };
  const storage = { getItem: (key) => saved.get(key), setItem: (key, value) => saved.set(key, value) };
  const environment = { document: { documentElement: root }, localStorage: storage,
    matchMedia: () => ({ matches: true }), innerWidth: 1200, innerHeight: 800 };
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
    await Promise.resolve();
    for (let i = 0; i < 5; i++) await toggleTheme(click, (theme) => changed.push(theme));
    assert.equal(starts, 1, "连点不叠加快照");
    assert.equal(animations[0].options.pseudoElement, "::view-transition-old(root)");
    assert.equal(animations[0].options.fill, 'both', '动画前后都保持裁切，防止快照全屏闪出');
    assert.equal(root.style['--theme-x'], '1010px');
    assert.match(animations[0].frames.clipPath[1], /circle\(0px at 1010px 60px\)/);
    const radius = Number(animations[0].frames.clipPath[0].match(/circle\(([\d.]+)px/)[1]);
    assert.ok(radius >= Math.hypot(1010, 740), "裁切半径覆盖最远角");
    release(); await first;
    const second = toggleTheme({ ...click, detail: 0, currentTarget: { getBoundingClientRect: () => ({ left: 900, top: 30, width: 80, height: 40 }) } }, () => {});
    await Promise.resolve();
    assert.equal(animations[1].options.pseudoElement, "::view-transition-new(root)");
    assert.equal(animations[1].frames.clipPath[0], "circle(0px at 940px 50px)", "键盘触发从按钮中心开始");
    const css = readFileSync(new URL('../src/styles.css', import.meta.url), 'utf8');
    assert.match(css, /theme-mode="light"\]::view-transition-new\(root\)\s*\{[^}]*clip-path:\s*circle\(0px/, 'ready 之前的新亮色快照必须有初始裁切');
    assert.doesNotMatch(css, /::view-transition-group\(root\)\s*\{[^}]*animation:\s*none/, '保留 group 的快照尺寸动画，避免旧快照消失而露出白底');
    assert.equal(animations[1].options.fill, 'both', '最终圆形覆盖要保留到快照层销毁');
    release(); await second;
    globalThis.matchMedia = () => ({ matches: true });
    await toggleTheme(click, () => {});
    assert.equal(starts, 2, "减少动效时直接切换");
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
  } finally {
    for (const [key, value] of Object.entries(original)) {
      if (value === undefined) delete globalThis[key]; else globalThis[key] = value;
    }
  }
});
