import { test } from 'node:test';
import assert from 'node:assert/strict';
import { buildSync } from 'esbuild';
import { fileURLToPath } from 'node:url';

const { outputFiles } = buildSync({ entryPoints: [fileURLToPath(new URL('../src/presentationNavigation.ts', import.meta.url))], bundle: true, write: false, format: 'esm' });
const nav = await import(`data:text/javascript;base64,${Buffer.from(outputFiles[0].text).toString('base64')}`);

test('普通入口及其他来源不出现汇报上下文', () => {
  for (const search of ['', '?returnSlide=teaching', '?from=other', '?from=Presentation']) assert.equal(nav.presentationContext(search), null);
  assert.equal(nav.demoWorkbenchHref(''), '/');
});

test('每个汇报入口刷新后保留当前章节，只返回本站固定汇报路径', () => {
  for (const slide of nav.presentationSlides) {
    const url = new URL(nav.presentationEntry('/', slide.id), 'http://localhost');
    assert.equal(nav.presentationContext(url.search), slide.id);
    assert.equal(nav.presentationHref(nav.presentationContext(url.search)), `/presentation/#${slide.id}`);
  }
  assert.equal(nav.presentationContext('?from=presentation&returnSlide=https://evil.example'), 'demo');
  assert.equal(nav.presentationContext('?from=presentation&returnSlide=../../login'), 'demo');
});

test('经demo进入工作台衔接教学进阶，独立普通链接不继承状态', () => {
  const demo = new URL(nav.presentationEntry('/demo/', 'demo'), 'http://localhost');
  const workbench = new URL(nav.demoWorkbenchHref(demo.search), demo);
  assert.equal(nav.presentationContext(workbench.search), 'teaching');
  assert.equal(nav.presentationContext(new URL('/', demo).search), null);
  assert.equal(nav.demoWorkbenchHref('?unrelated=1'), '/');
});
