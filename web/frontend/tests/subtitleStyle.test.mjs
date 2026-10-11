import { test } from 'node:test';
import assert from 'node:assert/strict';
import { buildSync } from 'esbuild';
import { fileURLToPath } from 'node:url';

async function load(name) {
  const { outputFiles } = buildSync({ entryPoints: [fileURLToPath(new URL(`../src/${name}.ts`, import.meta.url))], bundle: true, write: false, format: 'esm' });
  return import(`data:text/javascript;base64,${Buffer.from(outputFiles[0].text).toString('base64')}`);
}
const styles = await load('subtitleStyle');
const storage = await load('storage');

test('字幕样式默认值与局部配置兼容旧草稿', () => {
  assert.deepEqual(styles.readSubtitleStyle({}), styles.defaultSubtitleStyle);
  assert.equal(styles.readSubtitleStyle({ font_size: 48, text_color: '#abcdef' }).text_color, '#ABCDEF');
  assert.equal(styles.readSubtitleStyle({ line_spacing: 2.4 }).line_spacing, 2.4);
  for (const value of [{ line_spacing: 1.5 }, { line_spacing: Infinity }, { font_size: 1000 }, { font_name: 'Arial,Injected' }, { background_opacity: NaN }, { text_color: 'red' }, { margin_horizontal: 99 }, { enabled: 'false' }, { unknown: true }, { constructor: 1 }]) {
    assert.equal(styles.readSubtitleStyle(value), undefined);
  }
});

test('字幕背景颜色转换与透明度', () => {
  assert.equal(styles.subtitleBackground({ ...styles.defaultSubtitleStyle, background_color: '#123456', background_opacity: 0.5 }), 'rgba(18, 52, 86, 0.5)');
});

test('误填为接口地址的旧字体配置恢复自动选择并保留其他样式', () => {
  for (const font_name of ['https://api.deepseek.com/v1', 'file:///fonts/Arial.ttf', '/fonts/Arial.ttf', 'C:/fonts/Arial.ttf']) {
    const value = styles.readSubtitleStyle({ font_name, font_size: 31, background_opacity: 0, margin_bottom: 6 });
    assert.equal(value.font_name, '');
    assert.equal(value.font_size, 31);
    assert.equal(value.background_opacity, 0);
    assert.equal(value.margin_bottom, 6);
  }
  assert.equal(styles.readSubtitleStyle({ font_name: 'Microsoft YaHei' }).font_name, 'Microsoft YaHei');
});

test('字幕草稿刷新恢复且不保留无效设置', () => {
  const saved = new Map();
  globalThis.window = { localStorage: { getItem: (key) => saved.get(key) ?? null, setItem: (key, value) => saved.set(key, value), removeItem: (key) => saved.delete(key) } };
  storage.saveDraft({ preset: 'zh', subtitleStyle: { ...styles.defaultSubtitleStyle, font_size: 48 } });
  assert.equal(storage.loadDraft().subtitleStyle.font_size, 48);
  saved.set(storage.KEYS.draft, JSON.stringify({ subtitleStyle: { font_size: -1 } }));
  assert.equal(storage.loadDraft().subtitleStyle, undefined);
  delete globalThis.window;
});
