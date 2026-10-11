export const roles = ["text", "vision", "tts", "asr", "grounding", "avatar", "qa"] as const;
export type Role = typeof roles[number];
export const bailianSpeechModels = ['cosyvoice-v3.5-plus', 'cosyvoice-v3.5-flash', 'cosyvoice-v3-plus', 'cosyvoice-v3-flash', 'cosyvoice-v2'] as const;
export const roleLabels: Record<Role, string> = { text: "文字 / 课件", vision: "视觉 / 讲稿", tts: "配音", asr: "音轨 / 参考录音识别", grounding: "光标定位", avatar: "数字人", qa: "问答评价" };
export type Stage = { mode: "api" | "local"; provider: "bailian" | "openai_compatible" | "qwen3" | "gui_owl" | "musetalk";
  connection_id?: string; model: string; parameters: { resolution?: "480P" | "720P"; speech_rate?: number; temperature?: number; max_output_tokens?: number } };
export type EvaluationPolicy = { enabled: boolean; script_threshold: number; minimum_threshold: number; standard_threshold: number; question_count: number; max_auto_retries: number };
export const defaultEvaluation: EvaluationPolicy = { enabled: true, script_threshold: 80, minimum_threshold: 60, standard_threshold: 80, question_count: 20, max_auto_retries: 2 };
export type ModelConfig = { schema_version: 2; preset: "bailian_api" | "local_existing" | "custom";
  stages: Record<Exclude<Role, "avatar" | "qa">, Stage> & { qa?: Stage }; timing: { source: "tts_timestamp" | "local_forced_aligner" }; avatar: Stage & { enabled: boolean }; evaluation?: EvaluationPolicy };
export type Connection = { id: string; label: string; provider: "bailian" | "openai_compatible"; key_present: boolean;
  compatible_base_url: string; native_http_base_url?: string | null; websocket_url?: string | null; scope_id: string; source: "memory" | "env" };
export type ModelOptions = { implemented: Record<string, boolean>; recommended_models: Record<Role, string>;
  local_models: Partial<Record<Role, { provider: Stage["provider"]; model: string; implemented: boolean }>> };
export const recommended: Record<Role, string> = { text: "qwen3.8-flash", vision: "qwen3-vl-plus", tts: "cosyvoice-v3.5-plus",
  asr: "fun-asr", grounding: "qwen3-vl-plus", avatar: "wan2.2-s2v", qa: "qwen3-vl-plus" };

export function bailianEndpoints(base: string) {
  const url = new URL(base.trim());
  if (url.protocol !== "https:" || url.username || url.password || url.search || url.hash
    || !(url.hostname === "dashscope.aliyuncs.com" || url.hostname.endsWith(".dashscope.aliyuncs.com") || url.hostname.endsWith(".maas.aliyuncs.com"))) {
    throw new Error("请先填写百炼工作空间的 HTTPS 兼容 Base URL，再填入配音端点");
  }
  return { native: `${url.origin}/api/v1`, websocket: `${url.origin.replace(/^https:/, 'wss:')}/api-ws/v1/inference` };
}
const localModels: Partial<Record<Role, Stage>> = { tts: { mode: "local", provider: "qwen3", model: "qwen3tts", parameters: {} },
  asr: { mode: "local", provider: "qwen3", model: "qwen3-asr", parameters: {} },
  grounding: { mode: "local", provider: "gui_owl", model: "gui-owl", parameters: {} },
  avatar: { mode: "local", provider: "musetalk", model: "musetalk", parameters: {} } };

export function roleStage(config: ModelConfig, role: Role): Stage {
  return role === "avatar" ? config.avatar : role === 'qa' ? config.stages.qa ?? config.stages.vision : config.stages[role];
}

export function replaceStage(config: ModelConfig, role: Role, stage: Stage): ModelConfig {
  return { ...config, preset: "custom", ...(role === "avatar" ? { avatar: { ...stage, enabled: config.avatar.enabled } }
    : { stages: { ...config.stages, [role]: stage } }),
    ...(role === "tts" ? { timing: { source: stage.mode === "api" ? "tts_timestamp" : "local_forced_aligner" } } : {}) };
}

export function setTextParameter(config: ModelConfig, role: "text" | "vision" | "qa", name: "temperature" | "max_output_tokens", value?: number): ModelConfig {
  const stage = roleStage(config, role);
  const parameters = { ...stage.parameters };
  if (value === undefined) delete parameters[name];
  else parameters[name] = value;
  return replaceStage(config, role, { ...stage, parameters });
}

export function presetConfig(preset: "bailian_api" | "local_existing", connectionId = "", enabled = false, options?: ModelOptions): ModelConfig {
  const stages = Object.fromEntries(roles.map((role) => {
    const local = options?.local_models?.[role];
    const stage: Stage = (preset === "local_existing" || role === 'asr') && localModels[role]
      ? { ...(localModels[role] as Stage), ...(local ? { model: local.model, provider: local.provider } : {}), parameters: {} }
      : { mode: "api", provider: "bailian", model: options?.recommended_models?.[role] ?? recommended[role], connection_id: connectionId, parameters: {} };
    return [role, stage];
  })) as Record<Role, Stage>;
  const { avatar, ...rest } = stages;
  return { schema_version: 2, preset, stages: rest, timing: { source: preset === "bailian_api" ? "tts_timestamp" : "local_forced_aligner" }, avatar: { ...avatar, enabled }, evaluation: { ...defaultEvaluation } };
}

export function avatarConfigurationProblem(config: ModelConfig, hasPortrait: boolean, options?: ModelOptions): string | undefined {
  const avatar = config.avatar;
  if (!avatar.enabled) return;
  if (!hasPortrait) return "开启数字人必须上传肖像，不会自动跳过";
  if (avatar.mode === "api") {
    if (!options?.implemented.bailian_avatar || avatar.model !== "wan2.2-s2v") return "此数字人API适配器尚未实现，请选择已接入的Wan型号";
    if (!avatar.connection_id) return "请选择数字人的百炼连接";
  } else if (!options?.implemented.avatar_segmentation || avatar.model !== "musetalk") return "此本地数字人适配器尚未实现";
}

export function migratedConfig(text: string, vision: string, enabled: boolean): ModelConfig {
  const config = presetConfig("local_existing", "", enabled);
  config.stages.text = { mode: "api", provider: "openai_compatible", model: text, parameters: {} };
  config.stages.vision = { mode: "api", provider: "openai_compatible", model: vision, parameters: {} };
  config.preset = "custom";
  return config;
}

export function taskModelPayload(config?: { model_config_origin?: string; model_config?: ModelConfig; voice?: unknown }, apiKey = "", apiBase = "") {
  return config?.model_config_origin === "v2" ? { model_config: config.model_config, ...(config.voice ? { voice: config.voice } : {}) }
    : { api_key: apiKey || undefined, api_base: apiBase || undefined };
}

export function readModelConfig(value: unknown): ModelConfig | undefined {
  if (!value || typeof value !== "object") return;
  const raw = value as Record<string, any>;
  if (raw.schema_version !== 2 || !["bailian_api", "local_existing", "custom"].includes(raw.preset)
    || !["tts_timestamp", "local_forced_aligner"].includes(raw.timing?.source) || typeof raw.avatar?.enabled !== "boolean") return;
  const stages: Partial<Record<Role, Stage>> = {};
  for (const role of roles) {
    const stage = role === "avatar" ? raw.avatar : role === 'qa' ? raw.stages?.qa ?? raw.stages?.vision : raw.stages?.[role];
    if (!stage || !["api", "local"].includes(stage.mode) || !["bailian", "openai_compatible", "qwen3", "gui_owl", "musetalk"].includes(stage.provider)
      || typeof stage.model !== "string" || !stage.model || stage.model.length > 200 || /\s|[\x00-\x1f]/.test(stage.model)) return;
    if (stage.connection_id != null && (typeof stage.connection_id !== "string" || !/^[a-zA-Z0-9_-]{0,100}$/.test(stage.connection_id))) return;
    const parameters: Stage["parameters"] = {};
    for (const key of ["speech_rate", "temperature", "max_output_tokens"] as const) {
      const item = stage.parameters?.[key];
      if (typeof item === "number" && Number.isFinite(item)) parameters[key] = item;
    }
    if (role === "avatar" && stage.mode === "api" && ["480P", "720P"].includes(stage.parameters?.resolution)) parameters.resolution = stage.parameters.resolution;
    stages[role] = { mode: stage.mode, provider: stage.provider, model: stage.model, parameters,
      ...(stage.connection_id ? { connection_id: stage.connection_id } : {}) };
  }
  const { avatar, ...rest } = stages as Record<Role, Stage>;
  const evaluation = { ...defaultEvaluation };
  if (typeof raw.evaluation?.enabled === 'boolean') evaluation.enabled = raw.evaluation.enabled;
  for (const key of ['script_threshold', 'minimum_threshold', 'standard_threshold', 'question_count', 'max_auto_retries'] as const) {
    const limit = key === 'question_count' ? [4, 40] : key === 'max_auto_retries' ? [0, 3] : [0, 100];
    const item = raw.evaluation?.[key];
    if (Number.isInteger(item) && item >= limit[0] && item <= limit[1]) evaluation[key] = item;
  }
  if (!raw.evaluation && evaluation.enabled) rest.asr = { ...localModels.asr! };
  if (evaluation.minimum_threshold > evaluation.standard_threshold) evaluation.minimum_threshold = evaluation.standard_threshold;
  return { schema_version: 2, preset: raw.preset, stages: rest, timing: { source: raw.timing.source }, avatar: { ...avatar, enabled: raw.avatar.enabled }, evaluation };
}
