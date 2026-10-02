export const roles = ["text", "vision", "tts", "asr", "grounding", "avatar"] as const;
export type Role = typeof roles[number];
export const roleLabels: Record<Role, string> = { text: "文字 / 课件", vision: "视觉 / 讲稿", tts: "配音", asr: "参考录音识别", grounding: "光标定位", avatar: "数字人" };
export type Stage = { mode: "api" | "local"; provider: "bailian" | "openai_compatible" | "qwen3" | "gui_owl" | "musetalk";
  connection_id?: string; model: string; parameters: { speech_rate?: number; temperature?: number; max_output_tokens?: number } };
export type ModelConfig = { schema_version: 2; preset: "bailian_api" | "local_existing" | "custom";
  stages: Record<Exclude<Role, "avatar">, Stage>; timing: { source: "tts_timestamp" | "local_forced_aligner" }; avatar: Stage & { enabled: boolean } };
export type Connection = { id: string; label: string; provider: "bailian" | "openai_compatible"; key_present: boolean;
  compatible_base_url: string; native_http_base_url?: string | null; websocket_url?: string | null; scope_id: string; source: "memory" | "env" };
export type ModelOptions = { implemented: Record<string, boolean>; recommended_models: Record<Role, string>;
  local_models: Partial<Record<Role, { provider: Stage["provider"]; model: string; implemented: boolean }>> };
export const recommended: Record<Role, string> = { text: "qwen3.8-flash", vision: "qwen3-vl-plus", tts: "cosyvoice-v3.5-plus",
  asr: "fun-asr", grounding: "qwen3-vl-plus", avatar: "wan2.2-s2v" };
const localModels: Partial<Record<Role, Stage>> = { tts: { mode: "local", provider: "qwen3", model: "qwen3tts", parameters: {} },
  asr: { mode: "local", provider: "qwen3", model: "qwen3-asr", parameters: {} },
  grounding: { mode: "local", provider: "gui_owl", model: "gui-owl", parameters: {} },
  avatar: { mode: "local", provider: "musetalk", model: "musetalk", parameters: {} } };

export function roleStage(config: ModelConfig, role: Role): Stage {
  return role === "avatar" ? config.avatar : config.stages[role];
}

export function replaceStage(config: ModelConfig, role: Role, stage: Stage): ModelConfig {
  return { ...config, preset: "custom", ...(role === "avatar" ? { avatar: { ...stage, enabled: config.avatar.enabled } }
    : { stages: { ...config.stages, [role]: stage } }),
    ...(role === "tts" ? { timing: { source: stage.mode === "api" ? "tts_timestamp" : "local_forced_aligner" } } : {}) };
}

export function setTextParameter(config: ModelConfig, role: "text" | "vision", name: "temperature" | "max_output_tokens", value?: number): ModelConfig {
  const stage = config.stages[role];
  const parameters = { ...stage.parameters };
  if (value === undefined) delete parameters[name];
  else parameters[name] = value;
  return replaceStage(config, role, { ...stage, parameters });
}

export function presetConfig(preset: "bailian_api" | "local_existing", connectionId = "", enabled = true, options?: ModelOptions): ModelConfig {
  const stages = Object.fromEntries(roles.map((role) => {
    const local = options?.local_models?.[role];
    const stage: Stage = preset === "local_existing" && localModels[role]
      ? { ...(localModels[role] as Stage), ...(local ? { model: local.model, provider: local.provider } : {}), parameters: {} }
      : { mode: "api", provider: "bailian", model: options?.recommended_models?.[role] ?? recommended[role], connection_id: connectionId, parameters: {} };
    return [role, stage];
  })) as Record<Role, Stage>;
  const { avatar, ...rest } = stages;
  return { schema_version: 2, preset, stages: rest, timing: { source: preset === "bailian_api" ? "tts_timestamp" : "local_forced_aligner" }, avatar: { ...avatar, enabled } };
}

export function migratedConfig(text: string, vision: string, enabled: boolean): ModelConfig {
  const config = presetConfig("local_existing", "", enabled);
  config.stages.text = { mode: "api", provider: "openai_compatible", model: text, parameters: {} };
  config.stages.vision = { mode: "api", provider: "openai_compatible", model: vision, parameters: {} };
  config.preset = "custom";
  return config;
}

export function taskModelPayload(config?: { model_config_origin?: string; model_config?: ModelConfig }, apiKey = "", apiBase = "") {
  return config?.model_config_origin === "v2" ? { model_config: config.model_config }
    : { api_key: apiKey || undefined, api_base: apiBase || undefined };
}

export function readModelConfig(value: unknown): ModelConfig | undefined {
  if (!value || typeof value !== "object") return;
  const raw = value as Record<string, any>;
  if (raw.schema_version !== 2 || !["bailian_api", "local_existing", "custom"].includes(raw.preset)
    || !["tts_timestamp", "local_forced_aligner"].includes(raw.timing?.source) || typeof raw.avatar?.enabled !== "boolean") return;
  const stages: Partial<Record<Role, Stage>> = {};
  for (const role of roles) {
    const stage = role === "avatar" ? raw.avatar : raw.stages?.[role];
    if (!stage || !["api", "local"].includes(stage.mode) || !["bailian", "openai_compatible", "qwen3", "gui_owl", "musetalk"].includes(stage.provider)
      || typeof stage.model !== "string" || !stage.model || stage.model.length > 200 || /\s|[\x00-\x1f]/.test(stage.model)) return;
    if (stage.connection_id != null && (typeof stage.connection_id !== "string" || !/^[a-zA-Z0-9_-]{0,100}$/.test(stage.connection_id))) return;
    const parameters: Stage["parameters"] = {};
    for (const key of ["speech_rate", "temperature", "max_output_tokens"] as const) {
      const item = stage.parameters?.[key];
      if (typeof item === "number" && Number.isFinite(item)) parameters[key] = item;
    }
    stages[role] = { mode: stage.mode, provider: stage.provider, model: stage.model, parameters,
      ...(stage.connection_id ? { connection_id: stage.connection_id } : {}) };
  }
  const { avatar, ...rest } = stages as Record<Role, Stage>;
  return { schema_version: 2, preset: raw.preset, stages: rest, timing: { source: raw.timing.source }, avatar: { ...avatar, enabled: raw.avatar.enabled } };
}
