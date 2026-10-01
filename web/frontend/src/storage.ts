/* 浏览器本地存储的**唯一入口**。
 *
 * 为什么单独成模块，而不是在组件里直接写 localStorage：
 *   同一个信息一旦有多个读写点，就一定会漂移 —— 这个项目已经在后端踩过一次
 *   （续跑任务的产物目录"写入侧用 config.result_dir、读取侧各拼各的 <job_dir>/result"，
 *    导致产物列表为空、文件服务 404，见 web/app.py `_result_dir` 的注释）。
 *   凭据与"当前正在看哪个任务"都属于这类信息，所以收在一处，键名也只在这里定义。
 *
 * 安全边界（要如实告诉使用者，不能只在界面上写"仅存内存"）：
 *   localStorage 是**同源可见**的，本机/内网工具够用；
 *   将来若开放公网访问，必须换成服务端会话（HttpOnly cookie + 每用户隔离），
 *   不能靠 localStorage 存密钥。见 docs/前端体验改进方案.md 的 #5。
 */

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
  /* 是否记住。关掉时要**顺手把已存的清掉** —— 否则会出现
     "界面上没勾记住、但 key 还躺在本地"这种看不见的残留。 */
  remember: boolean;
};

export function loadCredentials(): Credentials {
  const remember = safeGet(KEYS.remember) !== "0";
  return {
    apiKey: remember ? safeGet(KEYS.apiKey) || "" : "",
    apiBase: (remember ? safeGet(KEYS.apiBase) : "") || "",
    remember,
  };
}

export function saveCredentials(c: Credentials) {
  safeSet(KEYS.remember, c.remember ? "1" : "0");
  safeSet(KEYS.apiKey, c.remember && c.apiKey ? c.apiKey : null);
  safeSet(KEYS.apiBase, c.remember && c.apiBase ? c.apiBase : null);
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
};

export function loadDraft(): Partial<Draft> {
  try {
    const raw = JSON.parse(safeGet(KEYS.draft) || "{}");
    if (!raw || typeof raw !== "object" || Array.isArray(raw)) return {};
    const draft: Record<string, unknown> = {};
    for (const key of ["preset", "scriptPrompt", "slidePrompt", "scriptTemplate", "slideTemplate",
      "presetSpec", "designPrompt", "uploadText", "paperName", "portraitName"]) {
      if (typeof raw[key] === "string") draft[key] = raw[key];
    }
    for (const key of ["slideLang", "scriptLang"]) {
      if (["zh", "en"].includes(raw[key])) draft[key] = raw[key];
    }
    if (["paper", "textbook", "story", "fiction_comic", "custom"].includes(raw.contentProfile)) {
      draft.contentProfile = raw.contentProfile;
    }
    if (["default", "preset", "design", "upload"].includes(raw.voiceMode)) draft.voiceMode = raw.voiceMode;
    for (const key of ["generateTalking", "ifTreeSearch"]) {
      if (typeof raw[key] === "boolean") draft[key] = raw[key];
    }
    if (Number.isInteger(raw.slideCount) && raw.slideCount >= 4 && raw.slideCount <= 40) {
      draft.slideCount = raw.slideCount;
    }
    return draft;
  } catch { return {}; }
}

export function saveDraft(draft: Draft) {
  safeSet(KEYS.draft, JSON.stringify(draft));
}
