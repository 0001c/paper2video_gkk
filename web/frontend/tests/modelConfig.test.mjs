import { test } from "node:test";
import assert from "node:assert/strict";
import { buildSync } from "esbuild";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

async function load(name) {
  const { outputFiles } = buildSync({ entryPoints: [fileURLToPath(new URL(`../src/${name}.ts`, import.meta.url))], bundle: true, write: false, format: "esm" });
  return import(`data:text/javascript;base64,${Buffer.from(outputFiles[0].text).toString("base64")}`);
}
const models = await load("modelConfig");
const storage = await load("storage");

test("配音端点使用同一工作空间，拒绝相似域名和携带凭据的地址", () => {
  assert.deepEqual(models.bailianEndpoints("https://llm-workspace.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"), {
    native: "https://llm-workspace.cn-beijing.maas.aliyuncs.com/api/v1", websocket: "wss://llm-workspace.cn-beijing.maas.aliyuncs.com/api-ws/v1/inference",
  });
  for (const base of ["https://maas.qianwenapi.com/api/v1", "https://evilmaas.aliyuncs.com/v1", "https://dashscope.aliyuncs.com.evil.invalid/v1", "https://user:secret@dashscope.aliyuncs.com/v1", "http://dashscope.aliyuncs.com/v1", "https://dashscope.aliyuncs.com/v1?key=secret"]) assert.throws(() => models.bailianEndpoints(base));
});

test("P4任务音色只取当前任务载荷，不借用新任务草稿或旧密钥", () => {
  const config = models.presetConfig("bailian_api", "task-connection", false);
  const voice = { kind: "cloud", resource_id: "voice_saved_task" };
  assert.deepEqual(models.taskModelPayload({ model_config_origin: "v2", model_config: config, voice }, "forbidden-old-key"), { model_config: config, voice });
  assert.equal(models.taskModelPayload({ model_config_origin: "legacy", voice }, "old-key").voice, undefined);
});

test("文字/视觉独立连接与参数覆盖不串线，清空只删除当前覆盖值", () => {
  const config = models.presetConfig("local_existing", "text-connection", false);
  config.stages.vision.connection_id = "vision-connection";
  config.stages.vision.mode = "local";
  const text = models.setTextParameter(config, "text", "max_output_tokens", 8192);
  const vision = models.setTextParameter(text, "vision", "temperature", 0.3);
  assert.equal(config.stages.text.parameters.max_output_tokens, undefined);
  assert.equal(vision.stages.vision.connection_id, "vision-connection");
  assert.equal(vision.stages.vision.mode, "local");
  assert.equal(vision.stages.text.parameters.max_output_tokens, 8192);
  assert.equal(vision.stages.vision.parameters.temperature, 0.3);
  const cleared = models.setTextParameter(vision, "text", "max_output_tokens");
  assert.deepEqual(cleared.stages.text.parameters, {});
  assert.equal(cleared.stages.vision.parameters.temperature, 0.3);
  assert.deepEqual(models.readModelConfig(vision), vision);
  assert.equal(models.taskModelPayload({ model_config_origin: "v2", model_config: vision }).model_config, vision);
});

test("预设保留唯一数字人开关，切换TTS派生时间轴，文本本地是兼容服务", () => {
  const api = models.presetConfig("bailian_api", "connection", false);
  const local = models.presetConfig("local_existing", "connection", api.avatar.enabled);
  assert.equal(local.avatar.enabled, false);
  assert.equal(local.stages.text.mode, "api");
  assert.equal(local.stages.tts.mode, "local");
  assert.equal(api.timing.source, "tts_timestamp");
  const mixed = models.replaceStage(api, "tts", local.stages.tts);
  assert.equal(mixed.preset, "custom");
  assert.equal(mixed.timing.source, "local_forced_aligner");
  assert.equal(mixed.stages.vision.model, api.stages.vision.model);
  assert.equal(models.presetConfig("local_existing", "", false, { implemented: {} }).stages.tts.model, "qwen3tts");
});

test("旧草稿保留型号但不借用百炼连接；任务载荷只使用自己的快照", () => {
  const migrated = models.migratedConfig("old-text", "old-vision", true);
  assert.equal(migrated.stages.text.model, "old-text");
  assert.equal(migrated.stages.vision.provider, "openai_compatible");
  assert.equal(migrated.stages.text.connection_id, undefined);
  const body = models.taskModelPayload({ model_config_origin: "v2", model_config: migrated }, "should-not-send", "wrong-base");
  assert.deepEqual(Object.keys(body), ["model_config"]);
  assert.equal(body.model_config, migrated);
  assert.equal(models.taskModelPayload({ model_config_origin: "legacy" }, "legacy", "base").api_key, "legacy");
});

test("移除历史浏览器Key，强制禁止记住，v2草稿只持久化白名单字段", () => {
  const memory = new Map();
  globalThis.window = { localStorage: { getItem: (key) => memory.get(key) ?? null,
    setItem: (key, value) => memory.set(key, value), removeItem: (key) => memory.delete(key) } };
  memory.set(storage.KEYS.apiKey, "historic-secret"); memory.set(storage.KEYS.remember, "1");
  assert.equal(storage.loadCredentials().apiKey, "");
  assert.equal(memory.has(storage.KEYS.apiKey), false);
  storage.saveCredentials({ apiKey: "new-secret", apiBase: "base", remember: true });
  assert.equal(memory.has(storage.KEYS.apiKey), false);
  const config = models.presetConfig("bailian_api", "connection", false);
  config.api_key = "root-secret"; config.stages.tts.parameters.api_key = "nested-secret";
  storage.saveDraft({ preset: "zh", scriptPrompt: "保留提示词", slideCount: 16, model_config: config, api_key: "draft-secret" });
  const saved = memory.get(storage.KEYS.draft);
  assert.equal(saved.includes("secret"), false);
  assert.equal(storage.loadDraft().model_config.stages.text.connection_id, "connection");
  assert.equal(storage.loadDraft().scriptPrompt, "保留提示词");
  assert.equal(JSON.parse(saved).schema_version, 2);
  config.avatar.enabled = "false";
  assert.equal(models.readModelConfig(config), undefined);
});

test("结构化422保留字段路径且不把服务端input当作错误文本", async () => {
  const { api } = await load("flow");
  globalThis.fetch = async () => ({ ok: false, status: 422, json: async () => ({ detail: {
    problems: [{ field: "config.model_config.stages.tts", message: "适配器未实现", input: "secret" }] } }) });
  await assert.rejects(api.post("/api/model-config/validate", {}), /config.model_config.stages.tts：适配器未实现/);
});

test("P7已接入数字人可准备课件，只有真实缺项拦截提交并保留输出分辨率", () => {
  const options = { implemented: { bailian_avatar: true, avatar_segmentation: true } };
  const api = models.presetConfig("bailian_api", "connection", true);
  api.avatar.parameters.resolution = "720P";
  assert.equal(models.avatarConfigurationProblem(api, true, options), undefined);
  assert.match(models.avatarConfigurationProblem(api, false, options), /肖像/);
  const missing = { ...api, avatar: { ...api.avatar, connection_id: "" } };
  assert.match(models.avatarConfigurationProblem(missing, true, options), /连接/);
  assert.match(models.avatarConfigurationProblem(api, true, { implemented: {} }), /尚未实现/);
  assert.equal(models.readModelConfig(api).avatar.parameters.resolution, "720P");
  const local = models.presetConfig("local_existing", "connection", true);
  assert.equal(models.avatarConfigurationProblem(local, true, options), undefined);
  assert.deepEqual(models.readModelConfig(local).avatar.parameters, {});
  const disabled = { ...api, avatar: { ...api.avatar, enabled: false, connection_id: "" } };
  assert.equal(models.avatarConfigurationProblem(disabled, false, options), undefined);
  const app = readFileSync(new URL('../src/App.tsx', import.meta.url), 'utf8');
  assert.equal(app.includes('等待P6'), false);
  assert.equal(app.includes('待P6适配'), false);
  assert.equal((app.match(/avatarConfigurationProblem\(modelConfig, !!portraitFile, opts\?\.model_configuration\)/g) ?? []).length, 2);
});
