import { test } from 'node:test';
import assert from 'node:assert/strict';
import { buildSync } from 'esbuild';
import { fileURLToPath } from 'node:url';

async function load(name) {
  const { outputFiles } = buildSync({ entryPoints: [fileURLToPath(new URL(`../src/${name}.ts`, import.meta.url))], bundle: true, write: false, format: 'esm' });
  return import(`data:text/javascript;base64,${Buffer.from(outputFiles[0].text).toString('base64')}`);
}
const design = await load('slideDesign');
const storage = await load('storage');
const base = 'My custom content instructions. Output complete \\documentclass{beamer} with frame environments.';

test('视觉方案兼容旧草稿，拒绝无效枚举与额外字段', () => {
  assert.equal(design.readSlideDesign(undefined), undefined);
  assert.deepEqual(design.readSlideDesign({}), design.defaultSlideDesign);
  assert.deepEqual(design.readSlideDesign({ theme: undefined }), design.defaultSlideDesign);
  assert.equal(design.readSlideDesign({ theme: 'warm' }).theme, 'warm');
  for (const value of [[], null, 'warm', { theme: 'unknown' }, { layout: '\\input{secret}' }, { ratio: '16:9' }, { density: null }, { font: 'Arial' }, { constructor: 1 }]) {
    assert.equal(design.readSlideDesign(value), undefined);
  }
});

test('实际生成提示词使用预览同源配色与版式，保留用户内容和 Beamer 要求', () => {
  for (const theme of design.slideThemes) {
    for (const layout of design.slideLayouts) {
      const value = { ...design.defaultSlideDesign, theme: theme.id, layout: layout.id };
      const result = design.composeSlidePrompt(base, value);
      assert.ok(result.startsWith(base));
      for (const key of ['background', 'ink', 'accent', 'panel']) assert.ok(result.includes(theme[key].slice(1)));
      assert.ok(result.includes(layout.instruction));
      assert.match(result, /aspectratio=169/);
      assert.match(result, /documentclass.*frame/);
    }
  }
});

test('再次调整替换生成方案，原始提示词不累积视觉指令', () => {
  const first = design.composeSlidePrompt(base, design.defaultSlideDesign);
  const changed = design.composeSlidePrompt(base, { ...design.defaultSlideDesign, theme: 'warm', ratio: 'standard', font: 'serif', density: 'airy' });
  assert.notEqual(first, changed);
  assert.match(changed, /aspectratio=43/);
  assert.match(changed, /font theme serif/);
  assert.match(changed, /Prefer 2 main points/);
  assert.equal((changed.match(/Presentation visual specification/g) || []).length, 1);
  assert.equal(base.includes('Presentation visual specification'), false);
});

test('完整视觉方案在草稿刷新后保留，非法配置不会存入', () => {
  const saved = new Map();
  globalThis.window = { localStorage: { getItem: key => saved.get(key) ?? null, setItem: (key, value) => saved.set(key, value), removeItem: key => saved.delete(key) } };
  try {
    const value = { ...design.defaultSlideDesign, theme: 'midnight', layout: 'compare', density: 'detailed', font: 'serif', ratio: 'standard' };
    storage.saveDraft({ slidePrompt: base, slideDesign: value });
    assert.deepEqual(storage.loadDraft().slideDesign, value);
    assert.equal(storage.loadDraft().slidePrompt, base);
    storage.saveDraft({ slideDesign: { theme: 'invalid' } });
    assert.equal(storage.loadDraft().slideDesign, undefined);
  } finally { delete globalThis.window; }
});
