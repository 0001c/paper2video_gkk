import { readModelConfig, type ModelConfig } from "./modelConfig";
import { readSubtitleStyle, type SubtitleStyle } from "./subtitleStyle";

const NS = "p2v";

export const KEYS = {
  apiKey: `${NS}.apiKey`,
  apiBase: `${NS}.apiBase`,
  remember: `${NS}.remember`,
  jobId: `${NS}.jobId`,
  draft: `${NS}.draft`,
  view: `${NS}.view`,
} as const;

/* 隐私模式 / 企业策略下 localStorage 可能直接抛异常。
   存储只是"方便"，绝不能因为它崩掉整个界面，所以读写全部包一层。 */
function safeGet(key: string): string | null {
  try {
    return window.localStorage.getItem(key);
  } catch {
    return null;
  }
}

function safeSet(key: string, value: string | null) {
  try {
    if (value === null) window.localStorage.removeItem(key);
    else window.localStorage.setItem(key, value);
  } catch {
    /* 存不了就算了：本次会话内仍然可用 */
  }
}

export type Credentials = {
  apiKey: string;
  apiBase: string;
  remember: boolean;
};

export function loadCredentials(): Credentials {
  safeSet(KEYS.apiKey, null);
  safeSet(KEYS.remember, null);
  return {
    apiKey: "",
    apiBase: safeGet(KEYS.apiBase) || "",
    remember: false,
  };
}

export function saveCredentials(c: Credentials) {
  safeSet(KEYS.remember, null);
  safeSet(KEYS.apiKey, null);
  safeSet(KEYS.apiBase, c.apiBase || null);
}

export function clearCredentials() {
  safeSet(KEYS.apiKey, null);
  safeSet(KEYS.apiBase, null);
}

/* 当前查看的任务 id：刷新页面后自动恢复到原来那个任务，
   否则每次 F5 都要回任务列表重新点一次"查看"。 */
export function loadJobId(): string | null {
  return safeGet(KEYS.jobId);
}

export function saveJobId(id: string | null) {
  safeSet(KEYS.jobId, id || null);
}

export function loadView(): "new" | "detail" | "history" {
  const view = safeGet(KEYS.view);
  return view === "detail" || view === "history" ? view : "new";
}

export function saveView(view: "new" | "detail" | "history") {
  safeSet(KEYS.view, view);
}

export type Draft = {
  schema_version?: 2;
  model_config?: ModelConfig;
  preset: string;
  slideLang: string;
  scriptLang: string;
  contentProfile: "paper" | "textbook" | "story" | "fiction_comic" | "custom";
  scriptPrompt: string;
  slidePrompt: string;
  scriptTemplate: string;
  slideTemplate: string;
  slideCount: number;
  generateTalking: boolean;
  ifTreeSearch: boolean;
  voiceMode: "default" | "preset" | "design" | "upload";
  presetSpec: string;
  designPrompt: string;
  uploadText: string;
  paperName: string;
  portraitName: string;
  courseEnabled?: boolean;
  courseSettings?: string;
  textModel?: string;
  visionModel?: string;
  scriptConcurrency?: number;
  cursorStyle?: string;
  cursorScale?: number;
  subtitleStyle?: SubtitleStyle;
};

export function loadDraft(): Partial<Draft> {
  try {
    const raw = JSON.parse(safeGet(KEYS.draft) || "{}");
    if (!raw || typeof raw !== "object" || Array.isArray(raw)) return {};
    const draft: Record<string, unknown> = {};
    const modelConfig = readModelConfig(raw.model_config);
    const subtitleStyle = readSubtitleStyle(raw.subtitleStyle);
    if (subtitleStyle) draft.subtitleStyle = subtitleStyle;
    if (modelConfig) { draft.schema_version = 2; draft.model_config = modelConfig; }
    for (const key of ["preset", "scriptPrompt", "slidePrompt", "scriptTemplate", "slideTemplate",
      "presetSpec", "designPrompt", "uploadText", "paperName", "portraitName", "courseSettings", "textModel", "visionModel", "cursorStyle"]) {
      if (typeof raw[key] === "string") draft[key] = raw[key];
    }
    for (const key of ["slideLang", "scriptLang"]) {
      if (["zh", "en"].includes(raw[key])) draft[key] = raw[key];
    }
    if (["paper", "textbook", "story", "fiction_comic", "custom"].includes(raw.contentProfile)) {
      draft.contentProfile = raw.contentProfile;
    }
    if (["default", "preset", "design", "upload"].includes(raw.voiceMode)) draft.voiceMode = raw.voiceMode;
    for (const key of ["generateTalking", "ifTreeSearch", "courseEnabled"]) {
      if (typeof raw[key] === "boolean") draft[key] = raw[key];
    }
    if (Number.isInteger(raw.slideCount) && raw.slideCount >= 4 && raw.slideCount <= 40) {
      draft.slideCount = raw.slideCount;
    }
    if (Number.isInteger(raw.scriptConcurrency) && raw.scriptConcurrency >= 1 && raw.scriptConcurrency <= 8) draft.scriptConcurrency = raw.scriptConcurrency;
    if ([0.75, 1, 1.5].includes(raw.cursorScale)) draft.cursorScale = raw.cursorScale;
    return draft;
  } catch { return {}; }
}

export function saveDraft(draft: Draft) {
  const allowed = ["preset", "slideLang", "scriptLang", "contentProfile", "scriptPrompt", "slidePrompt", "scriptTemplate", "slideTemplate",
    "slideCount", "generateTalking", "ifTreeSearch", "voiceMode", "presetSpec", "designPrompt", "uploadText", "paperName", "portraitName",
    "courseEnabled", "courseSettings", "textModel", "visionModel", "scriptConcurrency", "cursorStyle", "cursorScale"] as const;
  const clean = Object.fromEntries(allowed.filter((key) => draft[key] !== undefined).map((key) => [key, draft[key]]));
  const modelConfig = readModelConfig(draft.model_config);
  const subtitleStyle = readSubtitleStyle(draft.subtitleStyle);
  safeSet(KEYS.apiKey, null);
  safeSet(KEYS.draft, JSON.stringify({ ...clean, schema_version: 2, ...(modelConfig ? { model_config: modelConfig } : {}), ...(subtitleStyle ? { subtitleStyle } : {}) }));
}
