const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../assets/theme-toggle.js'), 'utf8');
const css = fs.readFileSync(path.join(__dirname, '../assets/theme-toggle.css'), 'utf8');
const tick = () => new Promise(setImmediate);

function fixture({ api = true, reduce = false, storageFails = false } = {}) {
  const classes = new Set(), active = new Set(), history = [], timers = new Map(), saved = new Map();
  let timerId = 0;
  const root = {
    dataset: {}, style: { setProperty(k, v) { this[k] = v; } },
    classList: { contains: k => classes.has(k), toggle(k, on) { if (on) classes.add(k); else classes.delete(k); } },
    setAttribute(k, v) { this[k] = v; },
    animate(frames, options) {
      if (root.failAnimation) throw Error('animation unavailable');
      const effect = { frames, options, cancel() { active.delete(effect); } };
      active.add(effect); return effect;
    },
  };
  const reduced = { matches: reduce };
  const dark = { matches: false, addEventListener() {} };
  const context = {
    document: { documentElement: root }, innerWidth: 1200, innerHeight: 800,
    matchMedia: text => text.includes('reduced') ? reduced : dark,
    localStorage: { getItem: k => { if (storageFails) throw Error('storage unavailable'); return saved.get(k); },
      setItem: (k, v) => { if (storageFails) throw Error('storage unavailable'); saved.set(k, v); } },
    setTimeout(fn) { const id = ++timerId; timers.set(id, fn); return id; }, clearTimeout(id) { timers.delete(id); },
  };
  context.window = context;
  let phase = 'normal';
  if (api) context.document.startViewTransition = commit => {
    if (phase === 'throws') throw Error('inactive document');
    let release;
    const transition = { commit, skipped: false,
      ready: phase === 'ready-hang' ? new Promise(() => {}) : phase === 'ready-reject' ? Promise.reject(Error('skip')) : Promise.resolve(),
      finished: new Promise(resolve => { release = resolve; }),
      skipTransition() { this.skipped = true; },
      release() { release(); },
    };
    if (phase !== 'ready-hang') commit();
    history.push(transition); return transition;
  };
  vm.runInNewContext(source, context);
  return { root, active, history, timers, reduced, api: context.ThemeToggle,
    phase(value) { phase = value; }, timeout() { assert.equal(timers.size, 1); [...timers.values()][0](); } };
}
const click = { detail: 1, clientX: 1010, clientY: 60 };

async function main() {
  const f = fixture();
  for (let i = 0; i < 6; i++) {
    const run = f.api.toggle(click); await tick();
    for (let j = 0; j < 5; j++) await f.api.toggle(click);
    assert.equal(f.history.length, i + 1, 'rapid clicks share one transition');
    assert.equal(f.active.size, 1);
    const effect = [...f.active][0];
    assert.equal(effect.options.fill, 'both');
    assert.equal(effect.options.pseudoElement, i % 2 ? '::view-transition-new(root)' : '::view-transition-old(root)');
    const radius = Math.max(...effect.frames.clipPath.map(frame => Number(frame.match(/circle\(([\d.]+)/)[1])));
    assert.ok(radius >= Math.hypot(1010, 740));
    assert.equal(f.root.dataset.themeTransition, i % 2 ? 'light' : 'dark');
    f.history.at(-1).release(); await run;
    assert.equal(f.active.size, 0, 'no filling animation remains');
    assert.equal(f.timers.size, 0);
    assert.equal(f.root.dataset.themeTransition, undefined);
  }
  const keyboard = f.api.toggle({ detail: 0, clientX: 0, clientY: 0,
    currentTarget: { getBoundingClientRect: () => ({ left: 900, top: 30, width: 80, height: 40 }) } });
  await tick(); assert.equal(f.root.style['--theme-x'], '940px'); assert.equal(f.root.style['--theme-y'], '50px');
  f.history.at(-1).release(); await keyboard;
  console.log('PASS repeated rounds, rapid-click lock, cleanup, direction, radius and keyboard center');

  for (const phase of ['ready-hang', 'finished-hang']) {
    const h = fixture(); h.phase(phase);
    const run = h.api.toggle(click); await tick(); h.timeout(); await run;
    assert.equal(h.api.current(), 'dark'); assert.ok(h.history[0].skipped);
    assert.equal(h.active.size, 0); assert.equal(h.timers.size, 0);
    h.reduced.matches = true; await h.api.toggle(click);
    assert.equal(h.api.current(), 'light', 'timeout releases lock');
    h.history[0].commit(); assert.equal(h.api.current(), 'light', 'late callback cannot change newer theme');
  }
  console.log('PASS ready/finished timeout, unlock and late callback');

  for (const phase of ['ready-reject', 'throws', 'animate-throws']) {
    const h = fixture(); h.phase(phase); h.root.failAnimation = phase === 'animate-throws';
    await h.api.toggle(click);
    assert.equal(h.api.current(), 'dark'); assert.equal(h.active.size, 0); assert.equal(h.timers.size, 0);
    h.reduced.matches = true; await h.api.toggle(click); assert.equal(h.api.current(), 'light');
  }
  console.log('PASS rejected ready, API throw and animation throw');
  for (const options of [{ api: false }, { reduce: true }, { api: false, storageFails: true }]) {
    const h = fixture(options); await h.api.toggle(click);
    assert.equal(h.api.current(), 'dark'); assert.equal(h.history.length, 0); assert.equal(h.root.style.colorScheme, 'dark');
  }
  console.log('PASS unsupported API, reduced motion and unavailable storage');
  assert.match(css, /data-theme-transition="light"[^}]+clip-path:\s*circle\(0px/);
  assert.doesNotMatch(css.split('@media')[0], /view-transition-group\(root\)[^}]+animation:\s*none/);
  console.log('PASS scoped initial clipping and default group geometry');
}
main().catch(error => { console.error(error); process.exitCode = 1; });
