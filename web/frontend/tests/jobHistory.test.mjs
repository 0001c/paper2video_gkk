import { test } from 'node:test';
import assert from 'node:assert/strict';
import { buildSync } from 'esbuild';
import { fileURLToPath } from 'node:url';

const { outputFiles } = buildSync({ entryPoints: [fileURLToPath(new URL('../src/jobLineage.ts', import.meta.url))], bundle: true, write: false, format: 'esm' });
const { groupJobHistory, jobDescendants } = await import(`data:text/javascript;base64,${Buffer.from(outputFiles[0].text).toString('base64')}`);

test('多轮重生成与分支归入原始任务，保留直接父子关系和生成先后', () => {
  const jobs = [{ id: 'branch', resumed_from: 'root' }, { id: 'grandchild', resumed_from: 'child' }, { id: 'child', resumed_from: 'root' }, { id: 'root' }];
  const before = structuredClone(jobs);
  const roots = groupJobHistory(jobs);
  assert.equal(roots.length, 1);
  assert.equal(roots[0].job.id, 'root');
  assert.deepEqual(roots[0].children.map(n => n.job.id), ['child', 'branch']);
  assert.deepEqual(jobDescendants(roots[0]).map(({ node, depth }) => [node.job.id, depth]), [['child', 1], ['grandchild', 2], ['branch', 1]]);
  assert.deepEqual(jobs, before);
});

test('任务组按最新衍生活动排序，同名素材的独立任务不合并', () => {
  const roots = groupJobHistory([{ id: 'a-child', resumed_from: 'a' }, { id: 'b', source_name: 'same.pdf' }, { id: 'a', source_name: 'same.pdf' }]);
  assert.deepEqual(roots.map(n => n.job.id), ['a', 'b']);
  assert.equal(roots[1].children.length, 0);
});

test('来源记录缺失时保留现有任务及其后代，不伪造初始任务', () => {
  const roots = groupJobHistory([{ id: 'child', resumed_from: 'orphan' }, { id: 'orphan', resumed_from: 'missing' }]);
  assert.equal(roots[0].job.id, 'orphan');
  assert.equal(roots[0].unavailableParent, 'missing');
  assert.equal(roots[0].children[0].job.id, 'child');
});

test('自引用和循环来源不会导致无限遍历或丢失任务', () => {
  const jobs = [{ id: 'a', resumed_from: 'b' }, { id: 'b', resumed_from: 'a' }, { id: 'self', resumed_from: 'self' }, { id: 'tail', resumed_from: 'b' }];
  const roots = groupJobHistory(jobs);
  const all = roots.flatMap(root => [root.job.id, ...jobDescendants(root).map(({ node }) => node.job.id)]);
  assert.deepEqual(all.sort(), jobs.map(j => j.id).sort());
  assert.equal(new Set(all).size, jobs.length);
});

test('空记录正常返回，显式创建时间用于兄弟任务排序', () => {
  assert.deepEqual(groupJobHistory([]), []);
  const [root] = groupJobHistory([{ id: 'later', resumed_from: 'root', created_at: '2026-10-17 12:00:00' }, { id: 'earlier', resumed_from: 'root', created_at: '2026-10-17 11:00:00' }, { id: 'root' }]);
  assert.deepEqual(root.children.map(n => n.job.id), ['earlier', 'later']);
});
