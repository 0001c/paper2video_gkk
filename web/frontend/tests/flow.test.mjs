import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { buildSync } from "esbuild";
import { fileURLToPath } from "node:url";

async function sourceModule(name) {
  const { outputFiles } = buildSync({ entryPoints: [fileURLToPath(new URL(`../src/${name}.ts`, import.meta.url))], bundle: true, write: false, format: "esm" });
  const code = outputFiles[0].text;
  return import(`data:text/javascript;base64,${Buffer.from(code).toString("base64")}`);
}
const { api, runExclusive, applyTemplate } = await sourceModule("flow");
const storage = await sourceModule("storage");

test("P4显式云动作传递防重键，普通请求仍保留JSON头", async () => {
  const original = globalThis.fetch;
  let saved;
  globalThis.fetch = async (path, init) => { saved = init; return new Response(JSON.stringify({ ok: true }), { status: 200 }); };
  try {
    await api.post('/api/cloud-voices/generate', { kind: 'design' }, { 'Idempotency-Key': 'offline-token-123' });
    assert.equal(saved.headers['Idempotency-Key'], 'offline-token-123');
    assert.equal(saved.headers['Content-Type'], 'application/json');
  } finally { globalThis.fetch = original; }
});

test("连续点击只执行一次，失败后释放加载状态并允许重试", async () => {
  const lock = { current: false }, busy = [], errors = [];
  let release, calls = 0;
  const task = () => { calls++; return new Promise((_, reject) => { release = reject; }); };
  const first = runExclusive(lock, (value) => busy.push(value), task, (error) => errors.push(error.message));
  await runExclusive(lock, (value) => busy.push(value), task, () => {});
  assert.equal(calls, 1);
  release(new Error("断网"));
  await first;
  assert.deepEqual(busy, [true, false]);
  assert.deepEqual(errors, ["断网"]);
  await runExclusive(lock, () => {}, async () => { calls++; }, () => {});
  assert.equal(calls, 2);
  assert.equal(lock.current, false);
});

test("HTTP 失败、非法 JSON 和断网不能被当作成功", async () => {
  const original = globalThis.fetch;
  try {
    globalThis.fetch = async () => new Response(JSON.stringify({ detail: "产物已变化" }), { status: 409 });
    await assert.rejects(api.post("/rerun", {}), /产物已变化/);
    globalThis.fetch = async () => new Response("not json");
    await assert.rejects(api.get("/options"), /无法读取/);
    globalThis.fetch = async () => { throw new TypeError("Failed to fetch"); };
    await assert.rejects(api.postForm("/ingest", new FormData()), /无法连接服务/);
  } finally { globalThis.fetch = original; }
});

test("未编辑模板跟随语言变化，手动编辑不会被覆盖", () => {
  assert.equal(applyTemplate("旧模板", "旧模板", "新模板"), "新模板");
  assert.equal(applyTemplate("我的要求", "旧模板", "新模板"), "我的要求");
  assert.equal(applyTemplate("", "旧模板", "新模板"), "新模板");
  assert.equal(applyTemplate("已保存的草稿", "", "新模板"), "已保存的草稿");
});

test("草稿恢复只接受已知字段和有效值；损坏或禁用的存储不使应用崩溃", () => {
  const memory = new Map();
  globalThis.window = { localStorage: {
    getItem: (key) => memory.get(key) ?? null,
    setItem: (key, value) => memory.set(key, value),
    removeItem: (key) => memory.delete(key),
  } };
  memory.set(storage.KEYS.draft, JSON.stringify({ slideCount: 20, slideLang: "zh",
    scriptLang: "invalid", scriptPrompt: "自定义", generateTalking: false,
    apiKey: "should-not-load", unexpected: "ignored" }));
  assert.deepEqual(storage.loadDraft(), { slideCount: 20, slideLang: "zh", scriptPrompt: "自定义", generateTalking: false });
  memory.set(storage.KEYS.draft, "broken json");
  assert.deepEqual(storage.loadDraft(), {});
  storage.saveView("history");
  assert.equal(storage.loadView(), "history");
  storage.saveCredentials({ apiKey: "test-key", apiBase: "test-base", remember: true });
  storage.saveCredentials({ apiKey: "test-key", apiBase: "test-base", remember: false });
  assert.equal(memory.has(storage.KEYS.apiKey), false);
  window.localStorage.getItem = () => { throw new Error("disabled"); };
  assert.deepEqual(storage.loadDraft(), {});
});
