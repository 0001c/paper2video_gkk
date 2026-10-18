import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { flushSync } from "react-dom";
import { Button, Dialog, MessagePlugin, Popup, Radio, RadioGroup, Select } from "tdesign-react";
import { loadCredentials, loadDraft, loadJobId, loadView, saveDraft, saveJobId, saveView } from "./storage";
import { api, applyTemplate, errorMessage, runExclusive } from "./flow";
import { CoursePanel, defaultCourse, type CourseSettings, type CoursePlan } from "./CoursePanel";
import { ReviewPanel } from "./ReviewPanel";
import { AvatarToggle } from './AvatarToggle';
import { EvaluationPanel, type EvaluationReport } from './EvaluationPanel';
import { EvaluationSettings } from './EvaluationSettings';
import { VideoVersions } from './VideoVersions';
import { NotificationSettings } from './NotificationSettings';
import { ThemeToggle } from "./ThemeToggle";
import { AccessControls } from './AccessGate';
import { ModelSettings } from "./ModelSettings";
import { CloudVoicePanel, type CloudVoice } from "./CloudVoicePanel";
import { TaskModelSettings } from "./TaskModelSettings";
import { ModelCosts } from './ModelCosts';
import { RemoteTasks, type RemoteTaskDetails } from './RemoteTasks';
import { splitEvaluationConfig, evaluationModelProblem, avatarConfigurationProblem, migratedConfig, presetConfig, roles, roleStage, taskModelPayload, type Connection, type ModelConfig, type ModelOptions } from "./modelConfig";
import { CursorPicker, type CursorOption } from "./CursorPicker";
import { SubtitleSettings } from "./SubtitleSettings";
import { defaultSubtitleStyle, readSubtitleStyle, type SubtitleStyle } from "./subtitleStyle";
import { Help, Section } from './Section';
import { SourceReview } from './SourceReview';
import { JobHistory, type HistoryJob } from './JobHistory';
import { ViewPages, ViewTabs, type ViewId } from './ViewNavigation';
import { SlideDesignPanel } from './SlideDesignPanel';
import { composeSlidePrompt, defaultSlideDesign, slideDesignSummary, type SlideDesign } from './slideDesign';

/* ─────────────────────────────────────────────────────────────
   接口层。三个注意点：
   1. 所有请求都是同源相对路径 —— 生产环境由 FastAPI 直接服务静态文件，
      开发环境走 vite 的 /api 代理。这样前端不需要知道后端地址。
   2. 本机有 TUN 代理（HTTP_PROXY=127.0.0.1:13960），浏览器对 localhost
      通常会自动绕过，但若出现 502，先怀疑代理（见 README）。
   3. GET 一律 `cache: "no-store"`。产物与进度是**每秒都在变**的实时数据，
      浏览器的启发式缓存会让"刚生成的产物"看不见 —— 表现与"要手动刷新页面"
      完全一样，最难查（后端明明已经返回了新列表）。
   ───────────────────────────────────────────────────────────── */
const reportError = (error: unknown) => { void MessagePlugin.error(errorMessage(error)); };

type Opt = { value: string; label: string };

/* 阶段表由后端下发（/api/options.phases）—— 步骤条渲染它，
   总进度也由后端算（progress.overall_pct），前端**不自己拼公式**。
   两边各存一份"阶段权重"是这类进度条最容易漂的地方。 */
type Phase = { key: string; label: string; weight: number; has_pages: boolean; unit: string };

/* 可选开关（/api/options.toggles）—— 同样由后端下发。
   开关的**语义**最容易在前后端各写一份之后悄悄分叉：后端改了默认值或成本口径，
   界面还在说旧话。所以这里只负责渲染，文案一个字都不本地硬编码。
   `default` 也是后端给的：它是"不传这个字段时后端会怎么做"的唯一真相。 */
type Toggle = {
  key: string;
  label: string;
  default: boolean;
  note?: string;
  hint?: string;
  cost?: string;
};

type ScriptPromptProfile = {
  value: "paper" | "textbook" | "story" | "fiction_comic" | "custom";
  label: string;
  desc: string;
  prompts: Record<string, string>;
  slide_prompts: Record<string, string>;
};

type Options = {
  model_configuration?: ModelOptions;
  cursors?: CursorOption[];
  presets: (Opt & { slide_lang: string; script_lang: string })[];
  slide_langs: Opt[];
  script_langs: Opt[];
  accept_formats: string[];
  phases: Phase[];
  /* 可选：后端老版本没有这个字段（静态前端由后端直接服务，两边短暂不同步是常态），
     所以标成可选并在渲染时 `?? []`，只少不崩。 */
  toggles?: Toggle[];
  script_prompt_profiles?: ScriptPromptProfile[];
  script_prompt_max_chars?: number;
  slide_prompt_max_chars?: number;
  slide_count?: { default: number; min: number; max: number };
  voice: Voices;
};

/* 音色：候选与"可提交的规格"都由后端组装好（`spec` 字段），前端只负责
   把选中的 spec 原样塞进 config。
   为什么这样切：预置 / 描述设计 / 上传三种来源的字段不同，若让前端也拼一遍，
   就成了"同一件事两处各拼一次"—— 这个项目已经在"产物路径"上踩过那个坑。 */
type VoiceSpec = {
  kind: "default" | "preset" | "design" | "upload" | "cloud";
  resource_id?: string;
  voice?: string;
  prompt?: string;
  path?: string;
  ref_text?: string | null;
};

type VoiceItem = {
  label: string;
  kind: string;
  lang?: string | null;
  gender?: string;
  duration_s?: number | null;
  sample_url?: string;
  ref_text?: string | null;
  spec: VoiceSpec;
  source_spec?: VoiceSpec;
};

type Voices = {
  available: boolean;
  by_lang: Record<string, { presets: VoiceItem[]; custom: VoiceItem[] }>;
  ref_texts: Record<string, string>;
  limits: { min_s: number; max_s: number };
  accept_audio: string[];
};

/* 本机配置 + 耗时标定值（来自 /api/system/info）。
   标定值由后端下发而不是前端写死：估算依据只保留一处来源，
   换机器标定时不用改前端。 */
type SysInfo = {
  os: string;
  python: string;
  cpu: { name: string; physical: number | null; logical: number | null };
  memory: { total_gb?: number; available_gb?: number };
  gpus: { name: string; vram_total_mb: number; vram_used_mb: number; util_percent: number }[];
  perf_factor: number | null;
  calibration: {
    en_per_slide_s: number;
    zh_per_slide_s: number;
    fixed_s: number;
    script_api_s: number;
    chars_per_slide: number;
    anchored_on: string;
    note: string;
  };
};

/* 进度对象全部由后端算好（read_progress），前端只负责显示。
   口径见 web/app.py 的 `_phase_overall_pct`：
     · overall_pct 是**阶段加权总进度**，单调不减 —— 不再出现"满格后又掉回 7%"；
     · 没有逐页进度的阶段不编造进度（只走到该阶段起点），
       "还活着"由 phase_elapsed_s / phase_expected_s 表达。 */
type Progress = {
  /* 当前阶段：key 给步骤条用，phase 是中文文案 */
  phase_key?: string;
  phase: string;
  phase_index?: number;
  phase_count?: number;
  /* 阶段加权总进度（0~100） */
  overall_pct?: number;
  /* 当前阶段自己的计数（**单位随阶段不同**：语音/数字人是"页"，光标定位是"条"）。
     光标阶段特别需要它：一次真实任务里有 18 条回退成 GUI-Owl 的指令、
     跑了约 9 分钟，而那时界面上只有"光标定位"四个字、日志也只在第 10 条时打一次，
     于是"在动"和"卡死"完全分不出来（使用者就是这么报的）。 */
  count_done?: number | null;
  count_total?: number | null;
  count_unit?: string | null;
  /* 兼容字段：后端仍下发 page_*（语义同 count_*） */
  page_done?: number | null;
  page_total?: number | null;
  phase_elapsed_s?: number | null;
  phase_expected_s?: number | null;
  phase_remaining_s?: number | null;
  script_concurrency?: number;
  /* 兼容字段（后端仍下发）：旧口径的页数计数 */
  tts_done: number;
  talking_done: number;
  total_pages: number;
  tail: string[];
  stalled_seconds: number | null;
  /* 卡住由**后端按阶段**判定：数字人阶段本来就几分钟没有新记录，
     用同一个阈值会在整个阶段持续误报「疑似卡住」。 */
  stalled?: boolean;
  stalled_limit_s?: number;
};

/* 新版字段缺失时的兜底（= 修复前的口径）。
 *
 * 为什么需要：静态前端是由后端**直接服务**的文件，浏览器一刷新就会拿到新界面，
 * 而服务进程可能还在跑旧代码 —— 两边短暂不同步是常态。这时 `overall_pct`
 * 不存在，若直接用 `?? 0` 就会显示一个假的 0%，反而更像坏掉了。
 * 退回旧口径不精确（会回跳/停在满格），但至少是个有意义的数。 */
function legacyPct(p?: Progress): number {
  if (!p) return 0;
  const total = p.total_pages || 0;
  const done = p.talking_done || p.tts_done || 0;
  return total ? Math.min(100, Math.round((done / total) * 100)) : 0;
}

type Job = {
  id: string;
  ref_img?: string;
  status: string;
  phase: string;
  evaluation?: { script?: EvaluationReport; video?: EvaluationReport };
  qa_error?: string;
  qa_decision?: string;
  qa_next_job_id?: string;
  notification?: { status: string; error?: string };
  phase_key?: string;
  remote_tasks?: RemoteTaskDetails;
  remote_cancel_state?: string;
  cancellation_requested?: boolean;
  exit_code?: number;
  elapsed_min?: number;
  progress?: Progress;
  queue_ahead?: number;
  /* 续跑出来的任务：值是原任务 id。产物与原任务**共享**
     （后端不复制 result 目录，只在 config.result_dir 里指过去）。 */
  resumed_from?: string;
  config?: { generate_talking?: boolean; content_profile?: string; slide_lang?: string;
    script_lang?: string; slide_count?: number; voice_note?: string; talking_note?: string;
    review_before_render?: boolean; target_minutes?: number | null; text_model?: string; vision_model?: string; script_concurrency?: number;
    cursor_style?: string; cursor_scale?: number; subtitle_style?: SubtitleStyle; model_config?: ModelConfig; model_config_origin?: string;
    voice?: VoiceSpec; voice_resource?: CloudVoice; qa_attempt?: number;
    model_connections?: Record<string, { label?: string; scope_id: string; provider: string; compatible_base_url: string; catalog_time?: string; catalog_source?: string }>; portrait_path?: string };
};

type RerunPlan = {
  audio_reuse?: { total_pages: number; reusable_pages: number; needs_synthesis: number; unavailable?: boolean } | null;
  model_source?: Job["config"];
  job_id: string;
  can_rerun: boolean;
  can_reuse_script: boolean;
  reason: string;
  token: string;
  source_name: string;
  overwrite: string[];
  generate_talking: boolean;
};

/** 实时连接的可用状态。断流必须**看得见** ——
 *  原先 onerror 里直接 close()，界面静默冻结，使用者只能靠"手动刷新"自救。
 *  但只有 retrying 才提示：刚切换任务时那一瞬的 connecting 不是故障。 */
type StreamState = "idle" | "connecting" | "live" | "retrying";

/* SSE 进度流封装成一个 hook。
   为什么单独抽出来：进度是全应用唯一「持续变化」的状态，
   集中在一处比散在各组件里更好维护。

   ⚠️ 重连是必须的（"产物要手动刷新"的根因之一）：
   `job.status` 是前端拉取产物/逐页素材的**唯一触发条件**，而 status 只能从
   这条流里更新。一旦流断了又不重连，status 就永远停在 "running"，
   那个触发条件再也不会到来 —— 表现为"跑完了但产物区一直不出现"。
   EventSource 本来自带重连，但原代码在 onerror 里 `close()` 把它关掉了。
   这里改成指数退避（1s → 2s → 5s，封顶 10s），并把连接状态暴露给界面。 */
function useJobStream(jobId: string | null, revision = 0) {
  const [job, setJob] = useState<Job | null>(null);
  const [streamState, setStreamState] = useState<StreamState>("idle");
  const [streamError, setStreamError] = useState("");
  const attemptRef = useRef(0);
  // 终态之后**不再重连**：服务端推完终态那一帧就会正常关闭流，浏览器把它当 error →
  // 若继续重连，已完成的任务会被每 5 秒重连一次，在服务端留下一串无意义的请求。
  const terminalRef = useRef(false);

  useEffect(() => {
    setJob(null);
    setStreamError("");
    attemptRef.current = 0;
    if (!jobId) {
      setJob(null);
      setStreamState("idle");
      return;
    }
    let alive = true;
    let timer: number | undefined;
    let es: EventSource | null = null;
    terminalRef.current = false;

    const open = () => {
      if (!alive || terminalRef.current) return;
      es = new EventSource(`/api/jobs/${jobId}/events`);
      es.onopen = () => {
        attemptRef.current = 0;
        setStreamState("live");
      };
      es.addEventListener("error", (event) => {
        // 后端的命名 error 帧与网络断流不同：不存在的任务不应无限重连。
        if (!(event instanceof MessageEvent) || !event.data || !alive) return;
        try {
          const data = JSON.parse(event.data);
          if (!data.error) return;
          terminalRef.current = true;
          if (timer) window.clearTimeout(timer);
          setStreamError(data.error);
          setStreamState("idle");
          es?.close();
        } catch { /* 非业务错误由 onerror 的退避处理 */ }
      });
      es.onmessage = (ev) => {
        try {
          const d = JSON.parse(ev.data);
          if (!alive) return;
          if (d.error) {
            setStreamError(d.error);
            terminalRef.current = true;
            setStreamState("idle");
            es?.close();
            return;
          }
          if (["done", "failed", "canceled", "awaiting_review"].includes(d.status)) terminalRef.current = true;
          setJob((prev) => ({ ...(prev?.id === jobId ? prev : {}), ...d, id: jobId }));
        } catch {
          /* 单条解析失败不该中断整个流 */
        }
      };
      es.onerror = () => {
        es?.close();
        es = null;
        if (!alive) return;
        if (terminalRef.current) {
          setStreamState("idle");
          return;
        }
        attemptRef.current += 1;
        setStreamState("retrying");
        const backoff = [1000, 2000, 5000][Math.min(2, attemptRef.current - 1)];
        timer = window.setTimeout(open, backoff);
      };
    };

    // 用 connecting 而不是 retrying：刚切换任务的这一瞬不是故障，
    // 直接报"连接已断开"会制造一次假警报（误报比不报更糟）。
    setStreamState("connecting");
    open();
    return () => {
      alive = false;
      if (timer) window.clearTimeout(timer);
      es?.close();
    };
  }, [jobId, revision]);

  return { job: job?.id === jobId ? job : null, streamState, streamError };
}

/* 拖放上传区 */
function Dropzone(props: {
  label?: string;
  hint: string;
  accept?: string;
  picked: string | null;
  onPick: (f: File) => void;
  disabled?: boolean;
}) {
  const [over, setOver] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);
  return (
    <div>
      {props.label && (
        <label style={{ display: "block", fontSize: 12, color: "var(--paper-ink-soft)", marginBottom: 5 }}>
          {props.label}
        </label>
      )}
      <div
        className={"dropzone" + (over ? " over" : "")}
        role="button"
        tabIndex={props.disabled ? -1 : 0}
        aria-label={props.label || props.hint}
        aria-disabled={props.disabled}
        onClick={() => !props.disabled && inputRef.current?.click()}
        onKeyDown={(e) => {
          if (!props.disabled && ["Enter", " "].includes(e.key)) {
            e.preventDefault();
            inputRef.current?.click();
          }
        }}
        onDragOver={(e) => {
          e.preventDefault();
          setOver(true);
        }}
        onDragLeave={() => setOver(false)}
        onDrop={(e) => {
          e.preventDefault();
          setOver(false);
          const f = e.dataTransfer.files?.[0];
          if (f && !props.disabled) props.onPick(f);
        }}
      >
        <div className="big">{props.picked ? "已选择" : "拖入文件，或点击选择"}</div>
        <div className="small">{props.hint}</div>
        {props.picked && <div className="picked">{props.picked}</div>}
      </div>
      <input
        ref={inputRef}
        type="file"
        disabled={props.disabled}
        accept={props.accept}
        style={{ display: "none" }}
        onChange={(e) => {
          const f = e.target.files?.[0];
          if (f) props.onPick(f);
          e.target.value = "";
        }}
      />
    </div>
  );
}

export default function App() {
  const initialDraft = useMemo(loadDraft, []);
  const [opts, setOpts] = useState<Options | null>(null);
  const [sys, setSys] = useState<SysInfo | null>(null);
  const [initError, setInitError] = useState("");
  const [sysError, setSysError] = useState("");
  const [jobsError, setJobsError] = useState("");
  const [filesError, setFilesError] = useState("");
  const [view, setView] = useState<ViewId>(loadView);
  useEffect(() => { saveView(view); }, [view]);
  const navigationRef = useRef<HTMLElement>(null);
  const [submitting, setSubmitting] = useState(false);
  const [submitHintVisible, setSubmitHintVisible] = useState(false);
  const [submitPhase, setSubmitPhase] = useState("");
  const submitLock = useRef(false);
  const ingestLock = useRef(false);
  const voiceLock = useRef(false);
  const rerunLock = useRef(false);
  const cancelLock = useRef(false);
  const initLock = useRef(false);
  const [initializing, setInitializing] = useState(false);
  const [canceling, setCanceling] = useState(false);
  const [rerunPlan, setRerunPlan] = useState<RerunPlan | null>(null);

  // ① 素材
  const [paperFile, setPaperFile] = useState<File | null>(null);
  const [portraitFile, setPortraitFile] = useState<File | null>(null);
  const [portraitInfo, setPortraitInfo] = useState<any | null>(null);
  const portraitSelection = useRef<File | null>(null);
  const portraitUpload = useRef<{ file: File; result: Promise<any> } | null>(null);
  const preparePortrait = useCallback(async () => {
    const file = portraitSelection.current;
    if (!file) return undefined;
    if (portraitUpload.current?.file !== file) {
      const form = new FormData(); form.append("paper", file);
      const result = api.postForm("/api/portrait", form).then(({ data }) => {
        if (!data.portrait_path) throw new Error(data.error || "人像上传失败");
        return data;
      });
      portraitUpload.current = { file, result };
    }
    let data;
    try { data = await portraitUpload.current.result; }
    catch (error) { if (portraitUpload.current?.file === file) portraitUpload.current = null; throw error; }
    if (portraitSelection.current !== file) throw new Error("人像已更换，请重新检查当前人像");
    setPortraitInfo(data);
    return data.portrait_path as string;
  }, []);
  const [ingest, setIngest] = useState<any>(null);
  const [ingesting, setIngesting] = useState(false);

  // ② 语言与类型
  const [preset, setPreset] = useState(initialDraft.preset ?? "zh");
  const [slideLang, setSlideLang] = useState(initialDraft.slideLang ?? "en");
  const [scriptLang, setScriptLang] = useState(initialDraft.scriptLang ?? "zh");
  const [contentProfile, setContentProfile] = useState<ScriptPromptProfile["value"]>(initialDraft.contentProfile ?? "paper");
  const [scriptPrompt, setScriptPrompt] = useState(initialDraft.scriptPrompt ?? "");
  const [slidePrompt, setSlidePrompt] = useState(initialDraft.slidePrompt ?? "");
  const [slideDesign, setSlideDesign] = useState<SlideDesign>(initialDraft.slideDesign ?? { ...defaultSlideDesign });
  const resolvedSlidePrompt = useMemo(() => composeSlidePrompt(slidePrompt, slideDesign), [slidePrompt, slideDesign]);
  const [slideCount, setSlideCount] = useState<number | "">(initialDraft.slideCount ?? 12);
  const [courseEnabled, setCourseEnabled] = useState(initialDraft.courseEnabled ?? initialDraft.contentProfile === "textbook");
  const [courseSettings, setCourseSettings] = useState<CourseSettings>(() => {
    try { return { ...defaultCourse, ...JSON.parse(initialDraft.courseSettings || "{}") }; }
    catch { return defaultCourse; }
  });
  const [coursePlan, setCoursePlan] = useState<CoursePlan | null>(null);
  const [modelConfig, setModelConfig] = useState<ModelConfig>(() => {
    const initial = initialDraft.model_config ?? (initialDraft.textModel || initialDraft.visionModel
      ? migratedConfig(initialDraft.textModel || "deepseek-flash", initialDraft.visionModel || "deepseek-flash", false)
      : presetConfig("bailian_api", "", false));
    return { ...initial, avatar: { ...initial.avatar, enabled: false } };
  });
  const generateTalking = modelConfig.avatar.enabled;
  const [taskOverride, setTaskOverride] = useState<{ jobId: string; value: ModelConfig } | null>(null);
  const [modelConnections, setModelConnections] = useState<Connection[] | null>(null);
  /* 幻灯片布局微调（树搜索）—— 默认关，与后端 `_build_argv` 的默认值一致。
     两边默认值必须相同：界面显示"未勾选"而后端当成 True 跑，就是在
     使用者不知情的情况下多花钱，且看不出来。 */
  const [ifTreeSearch, setIfTreeSearch] = useState(initialDraft.ifTreeSearch ?? false);

  const initialCreds = useMemo(loadCredentials, []);
  const [apiKey, setApiKey] = useState(initialCreds.apiKey);
  const [apiBase, setApiBase] = useState(initialCreds.apiBase);
  const textModel = modelConfig.stages.text.model;
  const visionModel = modelConfig.stages.vision.model;
  const [scriptConcurrency, setScriptConcurrency] = useState(initialDraft.scriptConcurrency ?? 3);
  const [cursorStyle, setCursorStyle] = useState(initialDraft.cursorStyle ?? 'laser-red');
  const [cursorScale, setCursorScale] = useState(initialDraft.cursorScale ?? 1);
  const [subtitleStyle, setSubtitleStyle] = useState<SubtitleStyle>(initialDraft.subtitleStyle ?? { ...defaultSubtitleStyle });
  const [taskSubtitleOverride, setTaskSubtitleOverride] = useState<{ jobId: string; value: SubtitleStyle } | null>(null);
  useEffect(() => {
    if (opts?.cursors?.length && !opts.cursors.some((item) => item.value === cursorStyle)) setCursorStyle('laser-red');
  }, [opts?.cursors, cursorStyle]);
  const [languageNotice, setLanguageNotice] = useState("");

  // ④ 运行
  // jobId 也持久化：刷新页面（或误关标签）后自动回到原来那个任务 ——
  // 否则每次都要回任务列表重新点一次"查看"，长任务里这很烦。
  const [jobId, setJobId] = useState<string | null>(loadJobId());
  const [streamRevision, setStreamRevision] = useState(0);
  const videoRef = useRef<HTMLVideoElement>(null);
  const [artifacts, setArtifacts] = useState<any[]>([]);
  // P1：任务记录 + 逐页素材
  const [jobs, setJobs] = useState<HistoryJob[]>([]);
  const [pages, setPages] = useState<any | null>(null);
  const [rerunning, setRerunning] = useState(false);
  // ④ 音色。四种来源最终都归结为"一份参考音频 + 它的参考文本"：
  //    default 用内置（零依赖）、preset 用 MiMo 预置音色、design 用描述生成、
  //    upload 用使用者自己的音频。生成与缓存都在后端（同一个音色只生成一次）。
  const [voiceMode, setVoiceMode] = useState<"default" | "preset" | "design" | "upload">(initialDraft.voiceMode ?? "default");
  const [cloudVoice, setCloudVoice] = useState<CloudVoice>();
  // 直接存整条候选（而不是只存音色名）：候选里已经带了后端组装好的 spec，
  // "上传的音频"这类候选根本没有音色名，只存名字就没法还原成 spec。
  const [presetPick, setPresetPick] = useState<VoiceItem | null>(null);
  const [designPrompt, setDesignPrompt] = useState(initialDraft.designPrompt ?? "");
  const [uploadSpec, setUploadSpec] = useState<VoiceSpec | null>(null);
  const [uploadText, setUploadText] = useState(initialDraft.uploadText ?? "");
  const [uploadInfo, setUploadInfo] = useState<any | null>(null);
  const [preview, setPreview] = useState<any | null>(null);
  const [voiceBusy, setVoiceBusy] = useState(false);
  const voiceContext = `${voiceMode}|${scriptLang}|${designPrompt}|${JSON.stringify(presetPick?.spec)}`;
  const voiceContextRef = useRef(voiceContext);
  voiceContextRef.current = voiceContext;
  useEffect(() => { setPreview(null); }, [voiceContext]);

  const { job, streamState, streamError } = useJobStream(jobId, streamRevision);
  const savedTaskModels = useMemo(() => job?.config?.model_config ? splitEvaluationConfig(job.config.model_config) : undefined, [job?.config?.model_config]);
  const taskModels = taskOverride && taskOverride.jobId === job?.id ? taskOverride.value : savedTaskModels;
  const [taskVoiceOverride, setTaskVoiceOverride] = useState<{ jobId: string; voice: VoiceSpec; resource?: CloudVoice }>();
  const taskVoice = taskVoiceOverride && taskVoiceOverride.jobId === job?.id ? taskVoiceOverride.voice : job?.config?.voice;
  const taskVoiceResource = taskVoiceOverride && taskVoiceOverride.jobId === job?.id ? taskVoiceOverride.resource : job?.config?.voice_resource;
  const [filesJobId, setFilesJobId] = useState<string | null>(null);
  const [filesVersion, setFilesVersion] = useState(0);
  const visibleArtifacts = filesJobId === jobId ? artifacts : [];
  const visiblePages = filesJobId === jobId ? pages : null;

  const selectJob = useCallback((id: string) => {
    setJobId(id);
    setStreamRevision((value) => value + 1);
    setView("detail");
  }, []);
  const followedRunningJob = useRef<string>();
  useEffect(() => {
    if (job && ['running', 'queued'].includes(job.status)) followedRunningJob.current = job.id;
    if (job?.qa_next_job_id && followedRunningJob.current === job.id) {
      followedRunningJob.current = job.qa_next_job_id;
      selectJob(job.qa_next_job_id);
    }
  }, [job?.id, job?.status, job?.qa_next_job_id, selectJob]);
  useEffect(() => {
    if (view === "detail") navigationRef.current?.scrollIntoView({
      behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth", block: "start",
    });
  }, [view, jobId]);

  // 候选音色：预置 + 我保存过的（按讲稿语言过滤，后端已组装好 spec）
  const voiceList: VoiceItem[] = useMemo(() => {
    const bucket = opts?.voice?.by_lang?.[scriptLang];
    if (!bucket) return [];
    return [...bucket.presets, ...bucket.custom];
  }, [opts, scriptLang]);

  const sameSpec = (a: VoiceSpec | null, b: VoiceSpec) =>
    JSON.stringify(a) === JSON.stringify(b);

  const restoredVoice = useRef(false);
  useEffect(() => {
    if (!opts || restoredVoice.current) return;
    restoredVoice.current = true;
    setPresetPick(voiceList.find((it) => JSON.stringify(it.spec) === initialDraft.presetSpec || (it.source_spec && JSON.stringify(it.source_spec) === initialDraft.presetSpec)) ?? null);
  }, [opts, voiceList, initialDraft.presetSpec]);

  // 讲稿语言一变，选中的预置音色很可能已不在候选里（中英是两组不同的音色），
  // 这时必须清掉 —— 否则会提交一个跨语言的音色，音色和发音都会打折扣。
  useEffect(() => {
    if (presetPick && !voiceList.some((it) => sameSpec(presetPick.spec, it.spec))) {
      setPresetPick(null);
      setPreview(null);
    }
  }, [voiceList, presetPick]);

  const voiceSpecNow = useCallback((): VoiceSpec => {
    if (modelConfig.stages.tts.mode === "api") return { kind: "cloud", resource_id: cloudVoice?.resource_id ?? "" };
    if (voiceMode === "preset") return presetPick?.spec ?? { kind: "default" };
    if (voiceMode === "design") return { kind: "design", prompt: designPrompt };
    if (voiceMode === "upload") {
      // 参考文本以输入框为准：使用者常常是"先传音频、想起文本再补"，
      // 不该逼他重新上传一遍。
      return uploadSpec ? { ...uploadSpec, ref_text: uploadText.trim() || null } : { kind: "default" };
    }
    return { kind: "default" };
  }, [voiceMode, presetPick, designPrompt, uploadSpec, uploadText, modelConfig.stages.tts.mode, cloudVoice]);

  const genVoice = useCallback(
    async (force: boolean) => runExclusive(voiceLock, setVoiceBusy, async () => {
      if (force || !presetPick?.sample_url || presetPick.spec.kind !== "upload") throw new Error("请选择已有参考音频的音色；本地模式只复用已保存样本");
      setPreview({ sample_url: presetPick.sample_url, duration_s: presetPick.duration_s,
        ref_text: presetPick.ref_text, cached: true });
    }, reportError),
    [presetPick],
  );

  const uploadVoice = useCallback(
    async (f: File) => runExclusive(voiceLock, setVoiceBusy, async () => {
      setUploadSpec(null);
      setUploadInfo(null);
      const form = new FormData();
      form.append("paper", f);                 // 字段名沿用上传接口的约定
      form.append("ref_text", uploadText);
      const { status, data } = await api.postForm("/api/voices/upload", form);
      if (status !== 200 || !data.ok) {
        setUploadSpec(null);
        setUploadInfo(null);
        return MessagePlugin.error(data.error || "参考音频上传失败");
      }
      setUploadSpec(data.spec);
      setUploadInfo(data);
      MessagePlugin.success(`参考音频已就绪（${data.duration_s ?? "?"} 秒）`);
      await refreshOptions();
    }, reportError),
    [uploadText],
  );

  async function refreshOptions() {
    try { setOpts(await api.get("/api/options")); setInitError(""); }
    catch (error) { setInitError(errorMessage(error)); }
  }
  const initialize = useCallback(() => runExclusive(initLock, setInitializing, async () => {
    await Promise.all([
      refreshOptions(),
      api.get("/api/system/info").then((data) => { setSys(data); setSysError(""); })
        .catch((error) => setSysError(errorMessage(error))),
    ]);
  }, reportError), []);
  useEffect(() => { void initialize(); }, [initialize]);

  // 选中任务变化就记住（null 表示回到未选中状态）。
  useEffect(() => {
    saveJobId(jobId);
  }, [jobId]);

  /* 耗时估算。
     依据是后端下发的标定值（来自本机两次真实运行的 stage 计时），
     不是拍脑袋的区间 —— 两处实测都能被这个式子复现：
       15 页英文讲稿 -> 871 s（实际 868）  11 页中文讲稿 -> 929 s（实际 934）
     页数由「已提取的正文字符数」粗估（实测 3540 字符 -> 11~15 页），
     所以只给区间、并注明是粗估；拿到真实页数后（stage 1 完成）本估算即失效。 */
  const est = useMemo(() => {
    const cal = sys?.calibration;
    if (!cal) return null;
    const rawPerSlide = scriptLang === "zh" ? cal.zh_per_slide_s : cal.en_per_slide_s;
    // 标定中数字人约占每页耗时的 38%；关闭数字人时扣掉这一段。
    const perSlide = rawPerSlide * (generateTalking && portraitFile ? 1 : 0.62);
    const slides = Number(slideCount) || 12;
    const used = slides;
    const factor = sys?.perf_factor ?? 1;
    const durationScale = courseEnabled && coursePlan ? Math.max(1, coursePlan.planned_minutes / (used * 0.5)) : 1;
    const seconds = (used * perSlide * durationScale + cal.fixed_s) * factor;
    return {
      slides,
      lo: Math.max(1, Math.round((seconds * 0.8) / 60)),
      hi: Math.max(2, Math.round((seconds * 1.25) / 60)),
      factor,
      anchoredOn: cal.anchored_on,
    };
  }, [sys, scriptLang, slideCount, generateTalking, portraitFile, courseEnabled, coursePlan]);

  // 预设变化时同步两个轴 —— 但要保留"单独指定某一轴"的能力，
  // 所以这里只在预设变化的那一刻同步，之后用户手动改的轴不再被覆盖。
  const appliedPreset = useRef(initialDraft.preset ?? "");
  useEffect(() => {
    if (appliedPreset.current === preset) return;
    const p = opts?.presets.find((x) => x.value === preset);
    if (p) {
      appliedPreset.current = preset;
      setSlideLang(p.slide_lang);
      setScriptLang(p.script_lang);
    }
  }, [preset, opts]);

  // 内容类型和讲稿语言共同决定初始模板。模板只是起点，textarea 中的内容会
  // 原样随任务保存；“恢复当前模板”也复用同一份后端下发值，避免前后端各维护一套。
  const promptTemplate = useMemo(() => {
    const profile = opts?.script_prompt_profiles?.find((x) => x.value === contentProfile);
    return profile?.prompts?.[scriptLang] ?? "";
  }, [opts, contentProfile, scriptLang]);

  const previousScriptTemplate = useRef(initialDraft.scriptTemplate ?? "");
  useEffect(() => {
    if (promptTemplate) {
      const previous = previousScriptTemplate.current;
      setScriptPrompt((current) => applyTemplate(current, previous, promptTemplate));
      previousScriptTemplate.current = promptTemplate;
    }
  }, [promptTemplate]);

  const slidePromptTemplate = useMemo(
    () => opts?.script_prompt_profiles
      ?.find((x) => x.value === contentProfile)
      ?.slide_prompts?.[slideLang] ?? "",
    [opts, contentProfile, slideLang],
  );

  const previousSlideTemplate = useRef(initialDraft.slideTemplate ?? "");
  useEffect(() => {
    if (slidePromptTemplate) {
      const previous = previousSlideTemplate.current;
      setSlidePrompt((current) => applyTemplate(current, previous, slidePromptTemplate));
      previousSlideTemplate.current = slidePromptTemplate;
    }
  }, [slidePromptTemplate]);

  useEffect(() => {
    saveDraft({ schema_version: 2, model_config: modelConfig, preset, slideLang, scriptLang, contentProfile, scriptPrompt, slidePrompt, slideDesign,
      scriptTemplate: promptTemplate || initialDraft.scriptTemplate || "",
      slideTemplate: slidePromptTemplate || initialDraft.slideTemplate || "",
      slideCount: Number(slideCount) || 12, generateTalking, ifTreeSearch, voiceMode,
      presetSpec: presetPick ? JSON.stringify(presetPick.spec) : initialDraft.presetSpec || "",
      designPrompt, uploadText, paperName: paperFile?.name || initialDraft.paperName || "",
      portraitName: portraitFile?.name || initialDraft.portraitName || "",
      courseEnabled, courseSettings: JSON.stringify(courseSettings), textModel, visionModel, scriptConcurrency, cursorStyle, cursorScale, subtitleStyle });
  }, [preset, slideLang, scriptLang, contentProfile, scriptPrompt, slidePrompt,
    promptTemplate, slidePromptTemplate, slideDesign, slideCount, generateTalking, ifTreeSearch, voiceMode,
    presetPick, designPrompt, uploadText, paperFile, portraitFile, initialDraft, courseEnabled, courseSettings, textModel, visionModel, scriptConcurrency, cursorStyle, cursorScale, subtitleStyle, modelConfig]);

  const doIngest = useCallback(async () => runExclusive(ingestLock, setIngesting, async () => {
    if (!paperFile) return MessagePlugin.warning("请先选择内容文件");
    setIngest(null);
    const form = new FormData();
    form.append("paper", paperFile);
    const { status, data } = await api.postForm("/api/ingest", form);
    if (status !== 200 || !data.ok) {
      setIngest({ error: data.error || "提取失败", hint: data.hint });
      return;
    }
    setIngest(data);
    setCoursePlan(null);
    setCourseSettings((s) => ({ ...s, section_ids: ["all"], sections: undefined }));
  }, reportError), [paperFile]);

  const useCorrectedSource = (data: any, file: File) => {
    setPaperFile(file); setIngest(data); setCoursePlan(null);
    setCourseSettings((settings) => ({ ...settings, section_ids: ['all'], sections: undefined }));
    setView('new');
  };

  const submit = useCallback(async () => runExclusive(submitLock, setSubmitting, async () => {
    setSubmitPhase("正在准备…");
    if (ingestLock.current || voiceLock.current) throw new Error("请等待文件提取或音色准备完成");
    if (!ingest || ingest.error) return MessagePlugin.warning("请先上传内容文件并完成提取预览");
    if (courseEnabled && !coursePlan) return MessagePlugin.warning("请预览并确认教案和时间分配");
    const evaluationProblem = evaluationModelProblem(modelConfig);
  if (evaluationProblem) missing.push(evaluationProblem);
  const avatarProblem = avatarConfigurationProblem(modelConfig, !!portraitFile, opts?.model_configuration);
    if (avatarProblem) return MessagePlugin.warning(avatarProblem);
    if (modelConfig.stages.tts.mode === "local" && (voiceMode === "design" || (voiceMode === "preset" && (!presetPick?.sample_url || presetPick.spec.kind !== "upload")))) return MessagePlugin.warning("本地配音请选择默认、已保存音色或上传参考录音");
    if (!scriptPrompt.trim()) return MessagePlugin.warning("讲稿提示词不能为空");
    if (!slidePrompt.trim() || resolvedSlidePrompt.length > (opts?.slide_prompt_max_chars ?? 30000)) {
      return MessagePlugin.warning("请填写有效的幻灯片提示词，并检查字符上限");
    }
    if (!Number.isInteger(slideCount) || Number(slideCount) < (opts?.slide_count?.min ?? 4)
        || Number(slideCount) > (opts?.slide_count?.max ?? 40)) {
      return MessagePlugin.warning("幻灯片页数必须为允许范围内的整数");
    }
    if (scriptPrompt.length > (opts?.script_prompt_max_chars ?? 20000)) {
      return MessagePlugin.warning(`讲稿提示词最多 ${opts?.script_prompt_max_chars ?? 20000} 字符`);
    }
    // 音色没选全就提交，会静默回落到内置音色 —— 使用者只会觉得"我选的音色没生效"。
    // 在这里拦住，比在成片里听出来划算得多。
    if (modelConfig.stages.tts.mode === 'local' && voiceMode === "upload" && !uploadSpec) {
      return MessagePlugin.warning("请先上传参考音频（或改回「内置默认音色」）");
    }

    // 人像走**专用接口**，但数字人是可选项：未勾选或没选人像时不上传，
    // 后端也会做同样的兜底，stage 3 改走“幻灯片 + 音频”直接合片。
    // ⚠️ 不能复用 /api/ingest：那个接口做的是"论文 → 正文提取"，
    // 拿图片进去必然报「无法识别的文件格式」—— 原先就是这么写的，
    // 导致人像永远传不上去、任务永远建不起来，界面上只看到"上传失败"。
    const talkingWillRun = generateTalking && !!portraitFile;
    let portraitPath: string | undefined;
    if (talkingWillRun && portraitFile) {
      setSubmitPhase("正在上传人像…");
      portraitPath = await preparePortrait();
    }

    await api.post("/api/model-config/validate", { model_config: modelConfig, voice: voiceSpecNow(),
      portrait_path: portraitPath, requested_stages: ["1", "2"], review_before_render: true });

    setSubmitPhase("正在创建任务…");
    const { status, data } = await api.post("/api/jobs", {
      ingest_id: ingest.ingest_id,
      portrait_path: portraitPath,
      paper_latex_root: ingest.latex_root || undefined,
      // 原始文件路径必须带上：PDF / Word 输入时后端要靠它先转成 LaTeX 工程
      // （流水线入口要「含 main.tex 的目录」）。只有 .tex 输入才不需要。
      source_path: ingest.source_path || undefined,
      course_plan_token: courseEnabled ? coursePlan?.token : undefined,
      config: {
        model_config: modelConfig,
        script_concurrency: scriptConcurrency,
        cursor_style: cursorStyle,
        cursor_scale: cursorScale,
        subtitle_style: subtitleStyle,
        stages: ["1", "2", "3"],
        slide_lang: slideLang,
        script_lang: scriptLang,
        paper_type: "auto",
        slide_count: slideCount,
        slide_prompt: resolvedSlidePrompt,
        content_profile: contentProfile,
        course: courseEnabled ? courseSettings : undefined,
        script_prompt: scriptPrompt,
        // 幻灯片布局微调（树搜索）。**必须显式传**：后端读不到这个字段时按
        // 默认 False 处理，但把"界面上的勾选状态"原样送到后端，才谈得上
        // "所见即所跑"。它也进了产物指纹，所以关/开产出的幻灯片不会互相命中。
        if_tree_search: ifTreeSearch,
        reuse_script: false,
        voice: voiceSpecNow(),
      },
    });
    if (status !== 200) return MessagePlugin.error(data.detail || "创建任务失败");
    selectJob(data.job_id);
    setArtifacts([]);
    // 音色的最终结论必须回显：使用者选了"冰糖"却听到原来的男声时，
    // 这一句就是唯一能解释原因的地方。
    if (data.voice_note) {
      if (data.voice_active) MessagePlugin.success(`已入队 · 音色：${data.voice_note}`);
      else MessagePlugin.info(`已入队 · 音色：${data.voice_note}`);
    }
    if (data.talking_note) MessagePlugin.info(data.talking_note);
  }, reportError), [ingest, portraitFile, preparePortrait, generateTalking, apiKey, apiBase, slideLang, scriptLang,
      contentProfile, scriptPrompt, slidePrompt, resolvedSlidePrompt, slideCount, opts, ifTreeSearch,
      voiceMode, presetPick, designPrompt, uploadSpec, uploadText, voiceSpecNow, selectJob,
      courseEnabled, coursePlan, courseSettings, textModel, visionModel, scriptConcurrency, cursorStyle, cursorScale, subtitleStyle, modelConfig]);

  // 任务列表：每 5 秒刷新。本地工具，这点开销可以忽略，
  // 换来的是"后端重启前留下的任务也能看到最新状态"。
  useEffect(() => {
    let alive = true;
    const load = () =>
      api.get("/api/jobs").then((d) => {
        if (alive) { setJobs(d.jobs || []); setJobsError(""); }
      }).catch((error) => { if (alive) setJobsError(errorMessage(error)); });
    load();
    const timer = window.setInterval(load, 5000);
    return () => { alive = false; window.clearInterval(timer); };
  }, []);

  // 产物与逐页素材的刷新。
  //
  // ⚠️ 三个触发点，缺一不可（使用者实测反馈："产物生成出来后不会直接显示，
  //    需要手动刷新页面才会显示"）：
  //      ① 换查看对象时立刻拉；
  //      ② 状态/阶段**每变一次**都拉 —— 阶段变化是"又出了新东西"的最好信号；
  //      ③ 运行期间**定时轮询**。这是关键：原先只依赖 `job.status` 这一个
  //         触发条件，而 status 只能从 SSE 流里更新 —— 流一断（或那一刻恰好
  //         没收到），这个条件就再也不会到来，产物区永久冻结。
  //         单点是脆弱设计，轮询是它的兜底；跑完自动停，开销可忽略。
  const runningNow = !!job && ["running", "queued"].includes(job.status);
  const phaseKeyNow = job?.progress?.phase_key || "";
  useEffect(() => {
    setFilesJobId(null);
    setArtifacts([]);
    setPages(null);
    setFilesError("");
    if (!jobId) {
      setArtifacts([]);
      setPages(null);
      return;
    }
    let alive = true;
    let loading = false;
    const load = async () => {
      if (loading) return;
      loading = true;
      try {
        const [files, pageData] = await Promise.all([
          api.get(`/api/jobs/${jobId}/artifacts`), api.get(`/api/jobs/${jobId}/pages`),
        ]);
        if (alive) {
          setArtifacts(files.artifacts || []);
          setPages(pageData);
          setFilesJobId(jobId);
          setFilesError("");
        }
      } catch (error) {
        if (alive) setFilesError(errorMessage(error));
      } finally { loading = false; }
    };
    load();
    let timer: number | undefined;
    if (runningNow) timer = window.setInterval(load, 5000);
    return () => {
      alive = false;
      if (timer) window.clearInterval(timer);
    };
  }, [jobId, job?.status, phaseKeyNow, runningNow, filesVersion]);

  const prepareRerun = useCallback(async (id: string) => runExclusive(rerunLock, setRerunning, async () => {
    const [plan, source] = await Promise.all([api.get(`/api/jobs/${id}/rerun-plan`), api.get(`/api/jobs/${id}`)]);
    plan.model_source = { ...source.config, ...(taskOverride?.jobId === id ? { model_config: taskOverride.value } : {}), ...(taskVoiceOverride?.jobId === id ? { voice: taskVoiceOverride.voice } : {}), ...(taskSubtitleOverride?.jobId === id ? { subtitle_style: taskSubtitleOverride.value } : {}) };
    setRerunPlan(plan);
  }, reportError), [taskOverride, taskVoiceOverride, taskSubtitleOverride]);

  const retryPreparation = () => runExclusive(rerunLock, setRerunning, async () => {
    if (!job || !window.confirm("继续准备课件与讲稿可能调用生成接口。已完成的课程讲稿页会复用，是否继续？")) return;
    const { data } = await api.post(`/api/jobs/${job.id}/prepare-retry`, taskModelPayload({ ...job.config, model_config: taskModels, voice: taskVoice }, apiKey, apiBase));
    selectJob(data.job_id);
  }, reportError);

  const doRerun = useCallback(async () => runExclusive(rerunLock, setRerunning, async () => {
    if (!rerunPlan) return;
    const { status, data } = await api.post(`/api/jobs/${rerunPlan.job_id}/rerun`, {
      ...taskModelPayload(rerunPlan.model_source, apiKey, apiBase),
      subtitle_style: rerunPlan.model_source?.subtitle_style,
      plan_token: rerunPlan.token,
      action: "continue",
      reuse_script: true,
      require_reuse_script: true,
    });
    if (status !== 200) {
      MessagePlugin.error(data.detail || "续跑失败");
      return;
    }
    setRerunPlan(null);
    selectJob(data.job_id);
    setArtifacts([]);
    setPages(null);
    MessagePlugin.success("已入队：复用讲稿及有效配音，从未完成的位置继续；变更或损坏的音频页会重新生成");
  }, reportError), [apiKey, apiBase, rerunPlan, selectJob]);

  // 产物链接：逐段转义，但要保留路径分隔符（数字人产物是多层目录）
  const fileUrl = useCallback(
    (rel: string) =>
      `/api/jobs/${jobId}/files/` + rel.split("/").map(encodeURIComponent).join("/"),
    [jobId],
  );

  // 状态点颜色：把后端的 status 映射到已有的 dot 样式
  const dotOf = (status: string) =>
    ({ done: "ok", running: "run", queued: "wait", canceled: "wait" } as Record<string, string>)[
      status
    ] || "err";

  const cancel = useCallback(async () => runExclusive(cancelLock, setCanceling, async () => {
    if (!jobId) return;
    if (!window.confirm(`取消任务 ${jobId}？已生成的产物会保留。`)) return;
    const { data } = await api.post(`/api/jobs/${jobId}/cancel`);
    MessagePlugin.info([data.warning, data.remote_error].filter(Boolean).join("；") || "已请求取消");
  }, reportError), [jobId]);

  // ── 进度：**全部由后端算**（overall_pct），前端不再自己拼 done/total ──
  // 旧公式 `talking_done || tts_done` 造成两个可见的错：幻灯片/讲稿阶段永远是 0%，
  // 数字人开工时从 100% 掉回 7%。现在用阶段加权（口径见 app.py 的 _phase_overall_pct）。
  const p = job?.progress;
  const pct = job?.status === "done" ? 100 : p?.overall_pct ?? legacyPct(p);
  const showTalkingPhase = job?.config?.generate_talking ?? (generateTalking && !!portraitFile);
  const taskSubtitleStyle = (taskSubtitleOverride?.jobId === job?.id ? taskSubtitleOverride?.value : readSubtitleStyle(job?.config?.subtitle_style)) ?? defaultSubtitleStyle;
  const phases: Phase[] = (opts?.phases ?? []).filter(
    (phase) => showTalkingPhase || phase.key !== "talking",
  );
  /* 树搜索开关的文案来自后端。取不到就整块不渲染（老后端），
     而不是用本地硬编码的文案顶上 —— 那样两边会各说各话。 */
  const treeToggle: Toggle | undefined =
    (opts?.toggles ?? []).find((t) => t.key === "if_tree_search");
  const curIdx = p?.phase_index ?? -1;
  const stalled = !!p?.stalled;
  const countTotal = p?.count_total ?? p?.page_total ?? 0;
  const countDone = p?.count_done ?? p?.page_done ?? 0;
  const countUnit = p?.count_unit || "页";

  // "已用 / 约 / 第 k 条"：没有计数的阶段**不编造进度**，改用时间说明"它还活着"。
  const fmtMin = (s: number | null | undefined) =>
    s == null ? "" : s >= 90 ? `${(s / 60).toFixed(1)} 分钟` : `${Math.round(s)} 秒`;
  const phaseHint = useMemo(() => {
    if (!p) return "";
    const parts: string[] = [];
    if (p.phase_elapsed_s != null) parts.push(`已用 ${fmtMin(p.phase_elapsed_s)}`);
    if (p.phase_key === "script") {
      parts.push(p.phase_remaining_s != null ? `预计剩余 ${fmtMin(p.phase_remaining_s)}（按本次速度动态估算）` : "完成首批页面后估算剩余时间");
      if (p.script_concurrency) parts.push(`并发 ${p.script_concurrency}`);
    } else if (p.phase_expected_s != null) parts.push(`阶段总耗时约 ${fmtMin(p.phase_expected_s)}（粗估）`);
    if (countTotal > 0) parts.push(`已完成 ${countDone} / ${countTotal} ${countUnit}`);
    return parts.join(" · ");
  }, [p, countTotal, countDone, countUnit]);

  const running = job && ["running", "queued"].includes(job.status);
  const missing: string[] = [];
  if (!opts || initError) missing.push("加载服务配置");
  if (opts && !opts.model_configuration?.recommended_models) missing.push("重载支持P2目录的后端");
  if (!ingest || ingest.error) missing.push("选择内容文件并完成提取预览");
  if (courseEnabled && !coursePlan) missing.push("预览并确认教案和时间分配");
  for (const role of roles) {
    if ((role === 'avatar' && !modelConfig.avatar.enabled) || (['qa', 'quiz'].includes(role) && !modelConfig.evaluation?.enabled)) continue;
    const stage = roleStage(modelConfig, role);
    if ((stage.mode === "api" || role === "text" || role === "vision" || (role === 'qa' || role === 'quiz')) && !stage.connection_id) {
      missing.push(`选择${role}连接（可重新应用预设填充）`);
    }
  }
  for (const role of ["text", "vision", "quiz", "qa"] as const) {
    if (['qa', 'quiz'].includes(role) && !modelConfig.evaluation?.enabled) continue;
    const stage = roleStage(modelConfig, role);
    const connection = modelConnections?.find((entry) => entry.id === stage.connection_id);
    if (!connection || (stage.mode === "api" && !connection.key_present)) missing.push(`${role}连接失效或缺密钥`);
  }
  if (!opts?.model_configuration?.implemented.independent_text_vision_connections
    && modelConfig.stages.text.connection_id !== modelConfig.stages.vision.connection_id) missing.push("P3前文字/视觉需共用连接");
  const evaluationProblem = evaluationModelProblem(modelConfig);
  if (evaluationProblem) missing.push(evaluationProblem);
  const avatarProblem = avatarConfigurationProblem(modelConfig, !!portraitFile, opts?.model_configuration);
  if (avatarProblem) missing.push(avatarProblem);
  if (modelConfig.stages.tts.mode === "api" && (!cloudVoice || cloudVoice.target_model !== modelConfig.stages.tts.model || cloudVoice.status !== "OK" || cloudVoice.timestamp_status !== "supported")) missing.push("选择可用且支持时间戳的云音色");
  if (modelConfig.stages.tts.mode === "local" && (voiceMode === "design" || (voiceMode === "preset" && presetPick && (!presetPick.sample_url || presetPick.spec.kind !== "upload")))) missing.push("本地请选择默认、已保存音色或上传录音");
  if (!textModel.trim() || !visionModel.trim()) missing.push("选择文字和视觉模型");
  if (!Number.isInteger(scriptConcurrency) || scriptConcurrency < 1 || scriptConcurrency > 8) missing.push("填写 1～8 的整数并发数");
  if (!slidePrompt.trim() || resolvedSlidePrompt.length > (opts?.slide_prompt_max_chars ?? 30000)) missing.push("检查幻灯片提示词（含风格设置后的总长度）");
  if (!scriptPrompt.trim() || scriptPrompt.length > (opts?.script_prompt_max_chars ?? 20000)) missing.push("检查讲稿提示词");
  if (!Number.isInteger(slideCount) || Number(slideCount) < (opts?.slide_count?.min ?? 4)
      || Number(slideCount) > (opts?.slide_count?.max ?? 40)) missing.push("填写有效的整数页数");
  if (modelConfig.stages.tts.mode === 'local' && voiceMode === "preset" && !presetPick) missing.push("选择音色");
  if (modelConfig.stages.tts.mode === 'local' && voiceMode === "design" && !designPrompt.trim()) missing.push("填写音色描述");
  if (modelConfig.stages.tts.mode === 'local' && voiceMode === "upload" && !uploadSpec) missing.push("上传参考音频");
  const ready = missing.length === 0 && !ingesting && !voiceBusy;
  const queuedCount = jobs.filter((item) => item.status === "queued").length;
  const activeCount = jobs.filter((item) => item.status === "running").length;

  // 核心数描述。实测本机 os.cpu_count / psutil / NUMBER_OF_PROCESSORS
  // 三者都是 8（BIOS 关了超线程），此时"8核/8线程"是冗余的，只报线程数。
  const cpuLine = useMemo(() => {
    const l = sys?.cpu.logical;
    if (!l) return "";
    const p = sys.cpu.physical;
    return p && p !== l ? `${p} 核 / ${l} 线程` : `${l} 线程`;
  }, [sys]);

  return (
    <div className="sheet">
      <header className="masthead">
        <div className="masthead-top">
          <div className="brand"><svg viewBox="0 0 32 32" fill="none" aria-hidden="true"><rect x="2" y="2" width="28" height="28" rx="4" stroke="currentColor" strokeWidth="1.5" /><path d="M10 10h7M10 14h4M10 18h4M19 15l7 4-7 4z" stroke="currentColor" strokeWidth="1.5" strokeLinejoin="round" /></svg><span>讲解工坊</span><span className="brand-caption">内容制作</span></div>
          <div className="masthead-actions"><AccessControls /><ThemeToggle /></div>
        </div>
        <div className="masthead-intro">
          <div><p className="eyebrow">CONTENT / VOICE / VIDEO</p><h1>内容讲解工作台</h1><p className="sub">选好素材与声音，让内容成为讲解视频。</p></div>
          <ol className="workflow-overview" aria-label="制作流程">
            <li><span>01</span><div><strong>编排内容</strong><small>素材、语言与声音</small></div></li>
            <li><span>02</span><div><strong>讲稿检查</strong><small>按配置评价或手动确认</small></div></li>
            <li><span>03</span><div><strong>视频验收</strong><small>配音合片与所选项目评价</small></div></li>
          </ol>
        </div>
        <details className="system-details">
          <summary><span className={"service-status" + (initError || sysError ? " has-error" : "")}><i />{initError ? "服务连接异常" : sysError ? "硬件信息异常" : opts ? "服务已连接" : "正在连接服务"}</span><span className="service-address">{window.location.hostname} · 单卡串行</span><span className="system-toggle-label">运行环境</span></summary>
        {/* 硬件栏：把"这台机器能跑多快"摆在心里有数的位置。
            页脚的预计时长就是从这三个数推出来的，所以放在一起看更合理。 */}
        <div className="hwbar">
          {sys ? (
            <>
              <span>
                <span className="k">CPU</span>
                {sys.cpu.name}
                {cpuLine ? ` · ${cpuLine}` : ""}
              </span>
              {sys.memory.total_gb ? (
                <span>
                  <span className="k">内存</span>
                  {sys.memory.total_gb} GB
                  <span className="k" style={{ marginLeft: 8 }}>可用</span>
                  {sys.memory.available_gb} GB
                </span>
              ) : null}
              {sys.gpus.length ? (
                <span>
                  <span className="k">GPU</span>
                  {sys.gpus[0].name}
                  {" · "}
                  {(sys.gpus[0].vram_total_mb / 1024).toFixed(0)} GB 显存
                  {" · 已用 "}
                  {(sys.gpus[0].vram_used_mb / 1024).toFixed(1)} GB
                </span>
              ) : (
                <span className="warn">未检测到 GPU —— 数字人与语音合成环节无法运行</span>
              )}
              <span>
                <span className="k">系统</span>
                {sys.os} · Python {sys.python}
              </span>
            </>
          ) : (
            <span>{sysError ? "暂时无法读取运行环境" : "正在读取本机配置…"}</span>
          )}
        </div>
        </details>
      </header>

      <ViewTabs view={view} hasJob={!!jobId} onChange={setView} navRef={navigationRef} />
      <NotificationSettings />
      {(initError || sysError) && (
        <div className="notice err" role="alert">
          <span>{initError ? `服务配置加载失败：${initError}` : `硬件信息读取失败：${sysError}`}</span>
          <Button variant="outline" loading={initializing} onClick={initialize}>重新连接</Button>
        </div>
      )}
      <ViewPages view={view}>
      <div>
      <div className="workspace-heading"><div><p className="eyebrow">NEW PROJECT</p><div className="draft-caption"><h2>编排新任务</h2><Help title="新任务草稿">这里的设置只用于新任务，不会更改已创建的任务。文字设置自动保存为本机草稿，文件需要重新选择。</Help></div></div><span className="draft-status">本机草稿</span></div>
      <p className="workspace-instruction">先上传并预览素材，再依次确认语言、模型与音色。完成设置后，在“生成与审核”中检查并创建任务。</p>
      {(!paperFile && initialDraft.paperName || !portraitFile && initialDraft.portraitName) && (
        <div className="notice info">
          <span>已恢复文字设置。浏览器不能恢复文件选择，请重新选择
            {!paperFile && initialDraft.paperName ? `内容文件「${initialDraft.paperName}」` : ""}
            {!portraitFile && initialDraft.portraitName ? ` 人像「${initialDraft.portraitName}」` : ""}。
          </span>
        </div>
      )}
      <div className="studio-layout">
      <aside className="studio-sidebar">
        <nav aria-label="新任务分区"><p className="sidebar-label">项目设置</p>
          {[
            ['material', '01', '素材'], ['credentials', '02', '模型配置'],
            ['language', '03', '语言与内容'], ['slides', '04', 'PPT 风格与布局'],
            ['teaching', '05', '教学范围'], ['voice', '06', '音色'], ['cursor', '07', '光标与字幕'], ['evaluation', '08', '评估配置'], ['prepare', '09', '生成与审核'],
          ].map(([anchor, number, title]) => <a key={anchor} href={'#' + anchor} onClick={(event) => {
            if (event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
            const section = document.getElementById(anchor);
            if (!section) return;
            event.preventDefault();
            const toggle = section.querySelector<HTMLButtonElement>('.section-toggle');
            if (toggle?.getAttribute('aria-expanded') === 'false') flushSync(() => toggle.click());
            history.replaceState(null, '', '#' + anchor);
            section.scrollIntoView({ behavior: matchMedia('(prefers-reduced-motion: reduce)').matches ? 'instant' : 'smooth', block: 'start' });
          }}><span>{number}</span>{title}</a>)}
        </nav>
        <div className="sidebar-queue"><p className="sidebar-label">运行队列</p><strong>{activeCount} <span>执行中</span><i /> {queuedCount} <span>排队中</span></strong></div>
        <p className="sidebar-note">课件与讲稿准备好后，<br />会暂停等待你的审核。</p>
      </aside>
      <div className="studio-content">
      <fieldset className="draft-fields" disabled={submitting}>
      {/* ① 素材 */}
      <Section anchor="material" number="01" title="素材" summary={paperFile?.name ?? '未选择内容文件'} important help={<>
        <p>支持 Word、PDF、LaTeX 或 ZIP，内容文件用于生成课件、讲稿、配音与字幕。上传后先提取并核对预览。</p>
        <p>相同文件按内容指纹复用已有提取结果，不会重复保存与解析。</p>
        <p>数字人只用于画面角落的小窗，人像与配音音色分别设置；未启用或未选人像时仍可输出完整讲解视频。</p>
      </>}>
        <div className="grid grid-2 material-grid">
          <div className="material-panel">
            <div className="material-heading">
              <span>内容文件</span>
              <span className="material-badge required">必需</span>
            </div>
            <Dropzone
              hint=".docx / .doc / .pdf / .tex / .zip / .txt"
              accept=".docx,.doc,.pdf,.tex,.zip,.txt"
              picked={paperFile?.name ?? null}
              disabled={ingesting || submitting}
              onPick={(f) => {
                if (ingestLock.current || submitLock.current) return;
                // 图片显然是误放（多半是想传人像）：当场说清，
                // 而不是传上去再由后端报"无法识别的文件格式"。
                if (/\.(png|jpe?g|webp|bmp|gif)$/i.test(f.name)) {
                  MessagePlugin.warning("这是图片 —— 请放到右侧「人像图」位置");
                  return;
                }
                setPaperFile(f);
                setIngest(null);
              }}
            />
          </div>
          <div className="material-panel">
            <div className="material-heading">
              <span>数字人小窗</span>
              <AvatarToggle value={modelConfig} onChange={setModelConfig} disabled={submitting} />
            </div>
            {generateTalking ? (
              <Dropzone
                hint="上传正面人像图；开启时必填，音频自动严格分段"
                accept="image/*"
                picked={portraitFile?.name ?? null}
                disabled={submitting}
                onPick={(f) => {
                  portraitSelection.current = f;
                  portraitUpload.current = null;
                  setPortraitFile(f);
                  setPortraitInfo(null); // 换图后旧的校验结果作废
                }}
              />
            ) : (
              <div className="material-skip">
                <strong>已关闭数字人</strong>
              </div>
            )}
            {/* 人像校验结果内联在 Dropzone 下方（不用弹窗，避免遮住刚要确认的东西） */}
            {portraitInfo && (
              <div className="notice ok" style={{ marginTop: 8, fontSize: 11.5 }}>
                <span>
                  已上传：{portraitInfo.format}
                  {portraitInfo.width ? ` · ${portraitInfo.width}×${portraitInfo.height}` : ""}
                  {portraitInfo.size_mb ? ` · ${portraitInfo.size_mb} MB` : ""}
                </span>
              </div>
            )}
            {portraitInfo?.warnings?.map((text: string) => (
              <div className="notice warn" key={text} style={{ marginTop: 8, fontSize: 11.5 }}>
                <span>{text}</span>
              </div>
            ))}
            {portraitInfo?.note && (
              <div className="hint" style={{ fontSize: 11, marginTop: 6 }}>
                {portraitInfo.note}
              </div>
            )}
          </div>
        </div>

        <div className="actions">
          <Button theme="primary" variant="outline" loading={ingesting} disabled={!paperFile || submitting || ingest?.reviewed_source} onClick={doIngest}>
            {ingest?.reviewed_source ? '修正正文已就绪' : '提取并预览'}
          </Button>
        </div>

        {ingest?.error && (
          <div className="notice err">
            <span><b>无法识别该文件</b>　{ingest.error}</span>
          </div>
        )}
        {ingest && !ingest.error && (
          <div className="preview">
            <div className="phead">
              <span>提取结果</span>
              <span className="sz" style={{ marginLeft: "auto", fontFamily: "var(--font-mono)", fontSize: 11.5 }}>
                {ingest.detected_format} · {ingest.chars} 字符
              </span>
            </div>
            <div className="pbody">{ingest.preview_head}</div>
          </div>
        )}
        {ingest?.lossy && (
          <div className="notice warn">
            <span><b>有损提取</b>　{ingest.hint || "该格式无法保留结构信息，请在预览中核对"}</span>
          </div>
        )}
        {!!ingest?.review_pages?.length && <p className="notice warn" role="status">源文第 {ingest.review_pages.join('、')} 页的文字或公式需要核对。请先修正正文，或选择不含缺失字形的教学范围；创建任务前会再次检查。</p>}
        {ingest?.preserved_glyph_images > 0 && <p className="notice info" role="status">已将 {ingest.preserved_glyph_images} 处公式或特殊字形按原图保留，可直接生成。公式图片会随正文进入课件；可展开源页查看。</p>}
        {ingest?.ingest_id && !ingest.error && ingest.chars > 0 && <SourceReview key={ingest.ingest_id} ingestId={ingest.ingest_id} onSaved={useCorrectedSource} preservedGlyphs={ingest.preserved_glyph_images} />}
        {/* 内容寻址：同一份文件之前在服务器上存在时直接复用提取结果。
            不说一句的话，"秒出结果"会被当成"没真提取"（实测容易误判）。 */}
        {ingest?.cached && !ingest.error && (
          <div className="notice info">
            <span><b>已复用提取结果</b> · 这份文件之前已上传</span>
          </div>
        )}
      </Section>

      {/* 模型决定音色路径：本地复用参考音频，API 选择独立云音色。 */}
      <Section anchor="credentials" number="02" title="模型配置" defaultOpen={false} summary={modelConfig.preset === 'bailian_api' ? '百炼API' : modelConfig.preset === 'local_existing' ? '本地模型' : '自定义组合'} important>
        {!initialDraft.model_config && (initialDraft.textModel || initialDraft.visionModel) && <p className="notice warn">已保留旧草稿的型号与本地路线，未沿用浏览器密钥。请添加兼容连接并重新选择；不会自动改成百炼默认。</p>}
        <ModelSettings value={modelConfig} options={opts?.model_configuration} concurrency={scriptConcurrency} onChange={setModelConfig} onConcurrency={setScriptConcurrency} voice={voiceSpecNow()} preparePortrait={preparePortrait} portraitKey={portraitFile} onConnectionsChange={setModelConnections} />
      </Section>

      {/* ③ 语言与类型 */}
      <Section anchor="language" number="03" title="语言与内容类型" defaultOpen={false} summary={`${slideLang === 'zh' ? '中文' : '英文'}课件 / ${scriptLang === 'zh' ? '中文' : '英文'}讲解 · ${slideCount || '待填'} 页`} help={<>
        <p>语言预设同时设置课件和讲稿语言，也可分别覆盖。幻灯片页数请在教学范围中设置。</p>
        <p>{(opts?.script_prompt_profiles ?? []).find((x) => x.value === contentProfile)?.desc}</p>
      </>}>
        <div className="grid grid-3">
          <div className="field">
            <label>语言预设</label>
            <Select
              value={preset}
              onChange={(v) => setPreset(v as string)}
              options={[...(opts?.presets ?? []).map((x) => ({ label: x.label, value: x.value })), { label: "自定义语言组合", value: "custom" }]}
              style={{ width: "100%" }}
            />
          </div>
          <div className="field">
            <label>幻灯片语言</label>
            <Select
              value={slideLang}
              onChange={(v) => { setSlideLang(v as string); setPreset("custom"); }}
              options={(opts?.slide_langs ?? []).map((x) => ({ label: x.label, value: x.value }))}
              style={{ width: "100%" }}
            />
          </div>
          <div className="field">
            <label>讲稿语言</label>
            <Select
              value={scriptLang}
              onChange={(v) => { setScriptLang(v as string); setPreset("custom"); }}
              options={(opts?.script_langs ?? []).map((x) => ({ label: x.label, value: x.value }))}
              style={{ width: "100%" }}
            />
          </div>
        </div>
        <div className="grid grid-2" style={{ marginTop: 16 }}>
          <div className="field">
            <label>讲解内容类型</label>
            <Select
              value={contentProfile}
              onChange={(v) => { setContentProfile(v as ScriptPromptProfile["value"]); setCourseEnabled(v === "textbook"); setCoursePlan(null); }}
              options={(opts?.script_prompt_profiles ?? []).map((x) => ({
                label: x.label,
                value: x.value,
              }))}
              style={{ width: "100%" }}
            />
          </div>

        </div>
        <Section title="高级设置：提示词与布局" compact defaultOpen={false} important help={<>
          <p>可编辑角色、受众、讲解详略及课件风格。自定义内容在切换语言或类型后保留，使用“应用当前模板”可以替换。</p>
          <p>课件输出必须保留 LaTeX Beamer 的 documentclass 与 frame；讲稿输出必须保留 ### 页分隔和“讲稿 | 光标目标”格式，否则字幕与光标无法解析。</p>
        </>}>
          {(scriptPrompt !== promptTemplate || slidePrompt !== slidePromptTemplate) && (
            <p className="hint">正在使用自定义提示词。切换语言或内容类型会保留你的编辑；如需新模板，请点击对应的“应用当前模板”。</p>
          )}
        <div className="field" style={{ marginTop: 16 }}>
          <div className="prompt-head">
            <label>幻灯片生成提示词（可编辑）</label>
            <span className="prompt-count">
              含风格设置 {resolvedSlidePrompt.length} / {opts?.slide_prompt_max_chars ?? 30000}
            </span>
          </div>
          <textarea
            className="prompt-editor slide-prompt-editor"
            rows={12}
            value={slidePrompt}
            onChange={(e) => setSlidePrompt(e.target.value)}
            placeholder="正在加载当前幻灯片语言的提示词模板…"
          />
          <div className="actions compact-actions">
            <Button
              variant="outline"
              size="small"
              disabled={!slidePromptTemplate || slidePrompt === slidePromptTemplate}
              onClick={() => setSlidePrompt(slidePromptTemplate)}
            >
              应用当前模板
            </Button>
          </div>
        </div>
        <div className="field" style={{ marginTop: 16 }}>
          <div className="prompt-head">
            <label>讲稿生成提示词（可编辑）</label>
            <span className="prompt-count">
              {scriptPrompt.length} / {opts?.script_prompt_max_chars ?? 20000}
            </span>
          </div>
          <textarea
            className="prompt-editor"
            rows={14}
            value={scriptPrompt}
            onChange={(e) => setScriptPrompt(e.target.value)}
            placeholder="正在加载当前内容类型的提示词模板…"
          />
          <div className="actions compact-actions">
            <Button
              variant="outline"
              size="small"
              disabled={!promptTemplate || scriptPrompt === promptTemplate}
              onClick={() => setScriptPrompt(promptTemplate)}
            >
              应用当前模板
            </Button>
          </div>
        </div>
        {/* 布局微调说明折叠进问号提示，避免一个复选框占据大段页面。 */}
        {treeToggle && (
          <div className="option-line" style={{ marginTop: 16 }}>
              <label className="checkline option-check">
                <input
                  type="checkbox"
                  checked={ifTreeSearch}
                  onChange={(e) => setIfTreeSearch(e.target.checked)}
                />
                <span>{treeToggle.label}</span>
              </label>
              <Help title="布局微调" important><p>{treeToggle.note}</p>{treeToggle.hint && <p>{treeToggle.hint}</p>}{treeToggle.cost && <p>额外消耗：{treeToggle.cost}</p>}</Help>
              {ifTreeSearch && <span className="option-active">已开启，将增加生成时间</span>}
          </div>
        )}
        </Section>


      </Section>
      <SlideDesignPanel value={slideDesign} onChange={setSlideDesign} language={slideLang} />
      <CoursePanel ingestId={ingest?.ingest_id} settings={courseSettings} onSettings={setCourseSettings}
        slideCount={slideCount} onSlideCount={setSlideCount} scriptLang={scriptLang} onPlan={setCoursePlan}
        enabled={courseEnabled} onEnabled={value => { setCourseEnabled(value); setCoursePlan(null); }}
        textStage={modelConfig.stages.text} textReady={!!modelConfig.stages.text.model && (modelConnections ?? []).some(connection => connection.id === modelConfig.stages.text.connection_id && connection.provider === modelConfig.stages.text.provider && (modelConfig.stages.text.mode === 'local' || connection.key_present))}
        languageAction={<><button type="button" className="linkbtn" onClick={() => {
          const already = slideLang === "zh" && scriptLang === "zh";
          setPreset(opts?.presets.find((v) => v.slide_lang === "zh" && v.script_lang === "zh")?.value ?? "custom");
          setSlideLang("zh"); setScriptLang("zh");
          setLanguageNotice(already ? "当前已是中文课件 + 中文讲解，无需更改。" : "已应用：中文课件 + 中文讲解，语言设置和默认模板已同步。自定义提示词保留，请重新确认教案。");
        }}>使用中文授课语言推荐</button>{languageNotice && <p className="notice info" role="status">{languageNotice}</p>}</>} />



      <Section anchor="voice" number="06" title="音色" defaultOpen={false} summary={modelConfig.stages.tts.mode === 'api' ? cloudVoice?.label ?? '待选择云音色' : voiceMode === 'default' ? '内置默认音色' : voiceMode === 'preset' ? presetPick?.label ?? '待选择音色' : voiceMode === 'design' ? '描述生成' : '上传参考音频'} important={voiceMode === 'upload'} help={<>
        <p>本地配音支持内置音色、以前保存的预置或设计样本，以及上传参考音频。选择已保存音色与试听都不会重新生成。</p>
        <p>预置及设计样本按讲稿语言筛选；上传音频需自行核对语言，跨语言克隆可能影响发音。API 配音请在云音色面板选择与当前连接、型号兼容的资源。</p>
      </>}>

        {modelConfig.stages.tts.mode === 'api' && <CloudVoicePanel stage={modelConfig.stages.tts} value={cloudVoice} onChange={setCloudVoice} />}
        <div hidden={modelConfig.stages.tts.mode === 'api'}>
        <div className="field">
          <label>讲稿由谁来讲</label>
          <p className="notice info">本地配音可用默认、已保存音色或上传参考录音。已保存样本可直接复用与试听；没有样本的预置暂不可选。</p>
          <RadioGroup value={voiceMode} onChange={(v) => { setVoiceMode(v as typeof voiceMode); setPreview(null); }}>
            <Radio value="default">内置默认音色（无需联网）</Radio>
            <Radio value="preset">
              预置 / 已保存音色
            </Radio>
            <Radio value="design" disabled>
              用一句话描述生成
            </Radio>
            <Radio value="upload">上传我的音频</Radio>
          </RadioGroup>
        </div>


        {voiceMode === "preset" && (
          <div className="grid grid-2 saved-voice-choice" style={{ marginTop: 12 }}>
            <div className="field">
              <div className="field-title"><label>选择音色</label></div>
              <Select
                value={presetPick ? JSON.stringify(presetPick.spec) : ""}
                onChange={(v) => {
                  const found = voiceList.find((it) => JSON.stringify(it.spec) === v) ?? null;
                  setPresetPick(found);
                  setPreview(null);
                }}
                options={voiceList.map((it) => ({
                  label: it.label + (it.sample_url ? (it.duration_s ? ` · ${it.duration_s}s` : " · 已保存") : " · 尚未保存"),
                  value: JSON.stringify(it.spec),
                  disabled: !it.sample_url || it.spec.kind !== "upload",
                }))}
                style={{ width: "100%" }}
              />
            </div>
            <div className="field">
              <div className="field-title"><label>试听</label><Help title="音色试听">同一个音色样本只生成一次，之后从缓存读取；可反复试听和使用。</Help></div>
              <div className="actions">
                <Button
                  variant="outline"
                  loading={voiceBusy}
                  disabled={!presetPick?.sample_url || presetPick.spec.kind !== "upload"}
                  onClick={() => genVoice(false)}
                >
                  试听已保存音色
                </Button>
              </div>
            </div>
          </div>
        )}

        {voiceMode === "design" && (
          <div style={{ marginTop: 12 }}>
            <div className="field">
              <label>音色描述（1~4 句）</label>
              <textarea
                className="voiceinput"
                rows={3}
                value={designPrompt}
                onChange={(e) => setDesignPrompt(e.target.value)}
                placeholder="例：三十多岁的女性学者，音色清亮而稳，语速适中，咬字清晰，像在讲台上讲课。"
              />
            </div>
            <div className="actions">
              <Button
                variant="outline"
                loading={voiceBusy}
                disabled={!designPrompt.trim()}
                onClick={() => genVoice(false)}
              >
                生成并试听
              </Button>
              <Button
                variant="outline"
                loading={voiceBusy}
                disabled={!designPrompt.trim() || !preview}
                onClick={() => genVoice(true)}
              >
                重新生成
              </Button>
            </div>
          </div>
        )}

        {voiceMode === "upload" && (
          <div className="grid grid-2" style={{ marginTop: 12 }}>
            <Dropzone
              label="参考音频"
              hint="5~15 秒、单人、无背景音乐；wav / mp3 / m4a / flac"
              accept="audio/*"
              disabled={voiceBusy || submitting}
              picked={uploadInfo ? `已就绪 · ${uploadInfo.duration_s} 秒` : null}
              onPick={uploadVoice}
            />
            <div className="field">
              <div className="field-title"><label>参考文本</label><Help title="参考文本" important>填写音频里说的话。留空将由 ASR 自动转写，转写错误可能影响声音克隆效果，建议核对后填写。</Help></div>
              <textarea
                className="voiceinput"
                rows={3}
                value={uploadText}
                onChange={(e) => setUploadText(e.target.value)}
                placeholder="把这段音频里说的话原样打出来"
              />
            </div>
          </div>
        )}

        {/* 试听：参考音频就是这个音色的定义，听到它=听到最终音色 */}
        {voiceMode !== "upload" && preview && (
          <div className="notice info" style={{ marginTop: 12 }}>
            <div className="statusline">
              <span className="dot ok" />
              <span>
                试听 · {preview.duration_s} 秒
                {preview.cached ? "（取自已保存的音色）" : "（刚生成，已保存）"}
              </span>
            </div>
            <audio controls src={preview.sample_url} style={{ width: "100%", marginTop: 6 }} />
            {preview.ref_text && <div className="hint" style={{ marginTop: 6 }}>参考文本：{preview.ref_text}</div>}
          </div>
        )}
        {voiceMode === "upload" && uploadInfo && (
          <div className="notice info" style={{ marginTop: 12 }}>
            <div className="statusline">
              <span className="dot ok" />
              <span>试听 · {uploadInfo.duration_s} 秒</span>
            </div>
            <audio controls src={uploadInfo.sample_url} style={{ width: "100%", marginTop: 6 }} />
          </div>
        )}
        {voiceMode === "upload" && uploadInfo?.warnings?.map((text: string) => (
          <div className="notice warn" key={text} style={{ marginTop: 8, fontSize: 11.5 }}>
            <span>{text}</span>
          </div>
        ))}

        {voiceMode === "upload" && !uploadSpec && initialDraft.voiceMode === "upload" && (
          <p className="hint">已恢复参考文本，请重新上传参考音频。</p>
        )}
        </div>
      </Section>

      <Section anchor="cursor" number="07" title="光标与字幕" defaultOpen={false} summary={opts?.cursors?.find((item) => item.value === cursorStyle)?.label ?? '红色光点'} help="选择视频光标与字幕样式，可在任务详情和审核合片前调整。只改变最终画面，不改变讲稿或音频。">
        <CursorPicker options={opts?.cursors ?? []} style={cursorStyle} scale={cursorScale} onStyle={setCursorStyle} onScale={setCursorScale} />
        <SubtitleSettings value={subtitleStyle} onChange={setSubtitleStyle} />
      </Section>

      </fieldset>
      <EvaluationSettings value={modelConfig} onChange={setModelConfig} />
      <Section anchor="prepare" number="09" title="生成与审核" defaultOpen={false} label="创建新任务" summary={missing.length ? '待完善设置' : '准备就绪'} important help={<>
        <p>先生成课件与讲稿。开启讲稿评价时，达到通过线后自动配音合片，否则等待复核或手动继续；仅选成片评价时，讲稿完成后继续生成。关闭质量评估时保持人工审核流程。</p>
        <p>成片达到标准线后完成；低于最低线时按错题反馈自动重生成，最多按设置次数执行；介于两条线之间时由你决定。自动重生成会新增模型费用。</p>
        <p>生成等待时间与成片播放时长分别计算。长课程尚无完整耗时标定；讲稿阶段动态估计剩余等待时间，配音合片以实际进度为准。新任务按队列串行执行。</p>
      </>}>
        {courseEnabled && <p>成片目标：{coursePlan ? `约 ${coursePlan.planned_minutes} 分钟` : "请先确认教案"}。</p>}
        <p>
          幻灯片：{slideLang === "zh" ? "中文" : "英文"} · 讲解：{scriptLang === "zh" ? "中文" : "英文"}
          {` · ${slideCount || "待填写"} 页 · 数字人${generateTalking && portraitFile ? "启用" : "关闭"}`}
          {` · 音色：${voiceMode === "default" ? "内置默认" : voiceMode === "preset" ? presetPick?.label || "未选择" : voiceMode === "design" ? "描述生成" : "上传参考音频"}`}
        </p>
        {generateTalking && !portraitFile && <p className="notice warn">开启数字人时肖像必填；上传肖像或关闭数字人后再创建任务。</p>}
        <p className="hint">PPT 方案：{slideDesignSummary(slideDesign)} · 可在“PPT 风格与布局”中再次调整。</p>
        {modelConfig.evaluation?.enabled && <p className="notice info"><strong>问答门禁已开启</strong> · 讲稿 {modelConfig.evaluation.script_threshold}% · 成片最低 {modelConfig.evaluation.minimum_threshold}% / 标准 {modelConfig.evaluation.standard_threshold}% · 自动重生成最多 {modelConfig.evaluation.max_auto_retries} 次</p>}
        <p className="hint">
          {!courseEnabled && est ? `本地配音与合片参考 ${est.lo}～${est.hi} 分钟（${est.slides} 页） · ` : ''}
          {`${activeCount} 个任务执行中 · ${queuedCount} 个排队中`}
        </p>
        <div className="actions">
          <Popup trigger="hover" visible={submitHintVisible} onVisibleChange={setSubmitHintVisible} placement="top" destroyOnClose overlayClassName="submit-hint-popup" content={<span role="status">{missing.length ? `还需：${missing.join("；")}` : ingesting || voiceBusy ? "正在准备素材，请稍候" : "素材已就绪，可以创建新任务"}</span>}>
          <span className="submit-trigger" tabIndex={!ready || submitting ? 0 : undefined} aria-label={!ready || submitting ? '查看生成任务准备情况' : undefined} onFocus={() => setSubmitHintVisible(true)} onBlur={() => setSubmitHintVisible(false)} onKeyDown={event => { if (event.key === 'Escape') setSubmitHintVisible(false); }}>
          <Button theme="primary" loading={submitting} disabled={!ready || submitting} onClick={submit}>
            {submitting ? submitPhase || "正在准备…" : "生成课件与讲稿"}
          </Button>
          </span></Popup>
        </div>
      </Section>
      </div>
      </div>
      </div>

      {/* 已创建任务的详情与新任务草稿独立 */}
      <section className="section">
        <div className="section-head">
          <h2 className="section-title">任务详情</h2>
          <span className="section-note">{jobId || "尚未选择任务"}</span>
        </div>

        <div className="actions">
          {running && <Button theme="danger" variant="outline" loading={canceling} onClick={cancel}>取消此任务</Button>}
          {job && !running && !job.config?.review_before_render && <Button variant="outline" loading={rerunning} onClick={() => prepareRerun(job.id)}>{job.status === 'done' ? '重新合成视频' : '继续生成视频'}</Button>}
          {job?.config?.review_before_render && ["failed", "canceled"].includes(job.status) && <Button variant="outline" loading={rerunning} onClick={retryPreparation}>继续准备课件与讲稿</Button>}
          {job?.queue_ahead ? (
            <span style={{ fontSize: 12, color: "var(--paper-ink-faint)" }}>
              队列中，前面还有 {job.queue_ahead} 个任务
            </span>
          ) : null}
        </div>
        {jobId && !job && !streamError && <p role="status">正在读取任务详情…</p>}
        {streamError && <div className="notice err" role="alert"><span>{streamError}，请到任务记录选择其他任务。</span></div>}
        {streamState === "retrying" && !job && <div className="notice warn">连接失败，正在重试。请检查服务是否运行。</div>}
        {job && <div className="task-overview"><div><span className="eyebrow">当前阶段</span><strong>{p?.phase || job.phase || job.status}</strong></div><p>{running ? '正在处理，可在下方查看生成进度。页面关闭后任务仍会继续。' : job.config?.review_before_render ? '先在下方核对课件、教学清单与口播，再保存并确认生成。' : job.status === 'done' ? '视频已生成，可查看成片、下载文件或核对逐页素材。' : '可查看进度与日志，核对已有素材后继续处理。'}</p></div>}
        {filesError && <div className="notice err" role="alert"><span>产物读取失败：{filesError}</span><Button variant="outline" onClick={() => setFilesVersion((value) => value + 1)}>重试读取</Button></div>}
        {job?.notification?.status === 'failed' && <p className="notice warn" role="status">{job.notification.error}</p>}
        {job?.status === 'failed' && job.phase_key === 'struct' && <div className="notice warn source-failure"><p>正文结构化未完成。若日志提示缺失字形，请对照源页修正，再以新素材创建任务。</p><SourceReview key={job.id} ingestId={job.id} onSaved={useCorrectedSource} /></div>}
        {job && <Section key={`settings-${job.id}`} title="任务设置与服务记录" compact defaultOpen={false} summary="字幕、模型、费用与历史版本">
        {job?.config && (
          <p className="hint">
            此任务：{job.config.slide_lang}/{job.config.script_lang}
            {` · 目标 ${job.config.slide_count || "未记录"} 页 · ${job.config.voice_note || "内置默认音色"}`}
            {` · ${job.config.talking_note || (job.config.generate_talking ? "已启用数字人" : "无数字人")}`}
            {` · 文字模型 ${job.config.model_config?.stages.text.model || job.config.text_model || "原默认模型"} · 讲稿模型 ${job.config.model_config?.stages.vision.model || job.config.vision_model || "原默认模型"}`}
            {` · 光标 ${opts?.cursors?.find((item) => item.value === (job.config?.cursor_style || 'classic-red'))?.label || '经典红箭头'} · ${Math.round((job.config.cursor_scale ?? 1) * 100)}%`}
            <br />这是创建时保存的配置，新建任务中的修改不影响它。
          </p>
        )}
        {job && <SubtitleSettings value={taskSubtitleStyle} disabled={!!running} onChange={(value) => setTaskSubtitleOverride({ jobId: job.id, value })} />}
        {job && taskModels && <EvaluationSettings value={taskModels} task disabled={!!running} onChange={(value) => setTaskOverride({ jobId: job.id, value })} />}
        {job && <VideoVersions jobId={job.id} refreshKey={`${job.status}-${filesVersion}-${job.qa_next_job_id ?? ''}`} />}
        {job && !running && <p className="hint">字幕样式修改将用于下一次审核合片或续跑，不会立即改变已有视频。</p>}
        {job?.config?.model_config_origin === "v2" && job.config.model_config && taskModels && <TaskModelSettings key={`models-${job.id}`}
          jobId={job.id} portraitPath={job.ref_img} snapshot={job.config.model_config} value={taskModels} connections={job.config.model_connections} options={opts?.model_configuration}
          voice={taskVoice} voiceResource={taskVoiceResource} onVoice={(resource) => setTaskVoiceOverride({ jobId: job.id, voice: resource ? { kind: 'cloud', resource_id: resource.resource_id } : { kind: 'default' }, resource })}
          concurrency={job.config.script_concurrency ?? 3} disabled={!!running} onChange={(value) => setTaskOverride({ jobId: job.id, value })} />}
        {job && job.config?.model_config_origin !== "v2" && <details><summary>旧任务凭据恢复（仅当前页面内存）</summary>
          <p className="hint">只用于旧任务继续/重跑，不影响v2草稿；刷新即清空，不做连接推理自检。</p>
          <div className="grid grid-2"><div className="field"><label htmlFor="legacy-key">旧任务 API Key</label><input id="legacy-key" type="password" autoComplete="off" value={apiKey} onChange={(event) => setApiKey(event.target.value)} /></div>
            <div className="field"><label htmlFor="legacy-base">旧任务 API Base</label><input id="legacy-base" value={apiBase} onChange={(event) => setApiBase(event.target.value)} /></div></div>
          <Button variant="text" onClick={() => { setApiKey(""); setApiBase(""); }}>清空本次凭据</Button>
        </details>}
        <ModelCosts url={`/api/jobs/${job.id}/model-costs`} />
        {(job.config?.model_config?.avatar.mode === "api" || !!job.remote_tasks?.items.length) && <RemoteTasks jobId={job.id} status={job.status} details={job.remote_tasks} />}
        </Section>}
        {job && !running && visiblePages?.pages?.length > 0 && <ReviewPanel key={`review-${job.id}`} jobId={job.id} apiKey={apiKey} apiBase={apiBase}
          modelConfig={taskModels} modelOrigin={job.config?.model_config_origin} voice={taskVoice} subtitleStyle={taskSubtitleStyle} concurrency={job.config?.script_concurrency ?? 3} cursors={opts?.cursors ?? []} cursorStyle={job.config?.cursor_style ?? 'classic-red'} cursorScale={job.config?.cursor_scale ?? 1} onJob={selectJob} onSaved={() => setFilesVersion((v) => v + 1)} />}

        {job && taskModels && (visiblePages?.pages?.length > 0 || job.phase_key === 'qa' || job.evaluation) && <EvaluationPanel key={`qa-${job.id}`}
          jobId={job.id} evaluation={job.evaluation} error={job.qa_error} decision={job.qa_decision} nextJobId={job.qa_next_job_id}
          attempt={job.config?.qa_attempt} value={taskModels} subtitleStyle={taskSubtitleStyle} busy={!!running} hasScript={!!visiblePages?.script_txt} hasVideo={!job.config?.review_before_render && visibleArtifacts.some((item) => item.name === '3_merage.mp4')} onJob={selectJob} />}

        {job && (
          <Section key={`progress-${job.id}-${!!running}`} title="生成进度" compact defaultOpen={!!running} summary={`${p?.phase || job.phase || job.status} · ${pct}%`}>
            <div className="statusline">
              <span className={"dot " + (job.status === "done" ? "ok" : job.status === "failed" ? "err" : "run")} />
              <span>{p?.phase || job.phase || job.status}</span>
              <span className="tail">
                {`总进度 ${pct}%`}
                {running && phaseHint ? `　${phaseHint}` : ""}
                {job.elapsed_min ? `　已跑 ${job.elapsed_min} min` : ""}
                {job.exit_code != null && job.exit_code !== 0 ? `　退出码 ${job.exit_code}` : ""}
              </span>
            </div>

            <div className="bar">
              <i style={{ width: `${pct}%` }} />
            </div>

            {/* 阶段步骤条：替代原先"只显示一个阶段名"的写法（那会让人以为卡住了）。
                三态一眼可见：已完成 / 进行中 / 未开始。 */}
            {phases.length > 0 && (
              <div className="steps">
                {phases.map((ph, i) => {
                  const state =
                    job.status === "done" || i < curIdx ? "done" : i === curIdx ? "cur" : "todo";
                  return (
                    <span
                      key={ph.key}
                      className={"step " + state}
                      title={`该阶段占预计总耗时的 ${Math.round(ph.weight * 100)}%`}
                    >
                      <span className="sdot" />
                      {ph.label}
                    </span>
                  );
                })}
              </div>
            )}

            {/* 计数格子只在"有计数"的阶段显示（语音/数字人=页，光标=条）。
                原先它一直挂着，幻灯片/讲稿/合片阶段是一排永远不动的空点。 */}
            {countTotal > 0 && (
              <div className="pips">
                {Array.from({ length: countTotal }).map((_, i) => (
                  <span key={i} className={i < countDone ? "done" : i === countDone ? "cur" : "todo"} />
                ))}
              </div>
            )}

            {/* 断流必须看得见：原先 onerror 直接 close()，界面静默冻结，
                使用者只能自己想到"刷新页面"（实测就是这么反馈的）。 */}
            {streamState === "retrying" && running && (
              <div className="notice warn" style={{ marginTop: 12 }}>
                <span>
                  <b>实时连接已断开，正在自动重连</b>　进度暂不会自动刷新；
                  产物列表仍在每 5 秒轮询一次，已完成的部分不会漏掉。
                </span>
              </div>
            )}

            {stalled && running && (
              <div className="notice warn" style={{ marginTop: 12 }}>
                <span>
                  <b>疑似卡住</b>　当前阶段已有 {Math.round((p?.stalled_seconds ?? 0) / 60)} 分钟无进度更新
                  （该阶段的判定阈值是 {Math.max(1, Math.round((p?.stalled_limit_s ?? 0) / 60))} 分钟）。
                  本机单卡执行时单页可能要数十秒；若持续不更新可取消后重跑。
                </span>
              </div>
            )}

            {p?.tail?.length ? <details><summary>运行日志 · 最近 {p.tail.length} 行</summary><div className="logtail">{p.tail.join("\n")}</div></details> : null}
          </Section>
        )}

        {/* 产物区**运行中也渲染**。原先外层是 `artifacts.length > 0 &&`，
            于是整块在运行期间不存在，"跑完才冒出来"看起来像丢了产物。 */}
        {job && (
          <div style={{ marginTop: 20 }}>
            <div className="statusline" style={{ marginBottom: 4 }}>
              <span className={"dot " + (visibleArtifacts.length ? (job.status === "done" ? "ok" : "run") : "wait")} />
              <span>产物</span>
              <span className="tail">
                {visibleArtifacts.length ? `${visibleArtifacts.length} 项` : "尚未生成"}
                {running && visibleArtifacts.length ? "　（每 5 秒刷新）" : ""}
              </span>
              {job.resumed_from && (
                <span className="section-note" style={{ marginLeft: 8 }}>
                  续跑自 {job.resumed_from}
                </span>
              )}
            </div>
            {visibleArtifacts.length > 0 ? (
              <details><summary>下载生成文件 · {visibleArtifacts.length} 项</summary><div className="artifacts">
                {visibleArtifacts.map((a) => (
                  <a key={a.name} href={`/api/jobs/${jobId}/files/${encodeURIComponent(a.name)}`} target="_blank" rel="noreferrer">
                    <span>{a.name}</span>
                    <span className="sz">{a.size_mb} MB</span>
                  </a>
                ))}
              </div></details>
            ) : (
              <div className="hint">
                产物会在各阶段结束后陆续出现在这里（运行中也能看到已生成的部分）。
              </div>
            )}
            {(() => {
              const video = visibleArtifacts.find((a) => a.name === "course_video.mp4") || visibleArtifacts.find((a) => a.name === "3_merage.mp4");
              return video ? (
                <video
                  key={video.version || video.size_bytes}
                  controls
                  ref={videoRef}
                  preload="metadata"
                  src={`/api/jobs/${jobId}/files/${video.name}?v=${encodeURIComponent(
                    video.version || String(video.size_bytes || "latest"),
                  )}`}
                />
              ) : null;
            })()}
            {visiblePages?.chapters?.length > 0 && <div className="actions" aria-label="课程章节导航">{visiblePages.chapters.map((c: { title: string; start_seconds: number }) => <button className="linkbtn" type="button" key={`${c.title}-${c.start_seconds}`} onClick={() => {
              if (videoRef.current) { videoRef.current.currentTime = c.start_seconds; videoRef.current.scrollIntoView({ block: "center" }); }
            }}>{Math.floor(c.start_seconds / 60)}:{String(Math.floor(c.start_seconds % 60)).padStart(2, '0')} {c.title}</button>)}</div>}
          </div>
        )}

        {/* P1 逐页素材浏览：服务端已把命名规则不一致的产物索引到一起
            （幻灯片 1.png 起、音频 0.wav 起、数字人还多一层参考图目录）。 */}
        {visiblePages?.total > 0 && (
          <Section title="逐页素材" compact defaultOpen={false} summary={`${visiblePages.total} 页 · 课件、音频与讲稿`}>
            <div className="statusline" style={{ marginBottom: 6 }}>
              <span className="dot ok" />
              <span>逐页素材</span>
              <span className="tail">共 {visiblePages.total} 页 · 点图看大图</span>
            </div>
            {visiblePages.slides_pdf || visiblePages.script_txt || visiblePages.cursor_json ? (
              <div className="filerefs">
                {visiblePages.slides_pdf && (
                  <a href={fileUrl(visiblePages.slides_pdf)} target="_blank" rel="noreferrer">
                    幻灯片总览 PDF
                  </a>
                )}
                {visiblePages.script_txt && (
                  <a href={fileUrl(visiblePages.script_txt)} target="_blank" rel="noreferrer">
                    讲稿（含光标短语）
                  </a>
                )}
                {visiblePages.cursor_json && (
                  <a href={fileUrl(visiblePages.cursor_json)} target="_blank" rel="noreferrer">
                    光标 JSON
                  </a>
                )}
              </div>
            ) : null}
            <div className="pagegrid">
              {visiblePages.pages.map((p: any) => (
                <div className="pagecell" key={p.page}>
                  {p.slide ? (
                    <a href={fileUrl(p.slide)} target="_blank" rel="noreferrer">
                      <img src={fileUrl(p.slide)} alt={`第 ${p.page} 页幻灯片`} loading="lazy" />
                    </a>
                  ) : (
                    <div className="ph">无图</div>
                  )}
                  <div className="pmeta">
                    <span>第 {p.page} 页</span>
                    {p.talking ? (
                      <a href={fileUrl(p.talking)} target="_blank" rel="noreferrer">
                        数字人
                      </a>
                    ) : null}
                    {p.audio ? (
                      <a href={fileUrl(p.audio)} target="_blank" rel="noreferrer">
                        音频
                      </a>
                    ) : null}
                  </div>
                  {p.script?.length ? (
                    <div className="pscript">
                      {p.script[0]}
                      {p.script.length > 1 ? ` …（共 ${p.script.length} 句）` : ""}
                    </div>
                  ) : null}
                </div>
              ))}
            </div>
          </Section>
        )}
      </section>

      {/* ⑤ 任务记录（P1）：续跑是省钱主力，界面上不做就浪费了 */}
      <section className="section">
        <div className="section-head">
          <h2 className="section-title">任务记录</h2>
          <span className="section-note">按初始任务分组 · 展开查看重新生成与续跑记录</span>
        </div>
        {jobsError && <div className="notice err" role="alert">任务记录读取失败：{jobsError}；每 5 秒自动重试。</div>}
        {jobs.length === 0 ? (
          <div className="hint">还没有任务。</div>
        ) : (
          <JobHistory jobs={jobs} selectedId={jobId} rerunning={rerunning} onSelect={selectJob} onRerun={prepareRerun} dotOf={dotOf} />
        )}
      </section>

      </ViewPages>
      <Dialog
        header="确认继续生成视频"
        visible={!!rerunPlan}
        onClose={() => { if (!rerunning) setRerunPlan(null); }}
        onConfirm={doRerun}
        confirmLoading={rerunning}
        confirmBtn={{ content: "复用讲稿与配音，继续视频", disabled: !rerunPlan?.can_rerun || !rerunPlan?.can_reuse_script }}
        cancelBtn={{ content: "返回", disabled: rerunning }}
        closeOnOverlayClick={false}
      >
        {rerunPlan && <>
          <p>{rerunPlan.source_name || rerunPlan.job_id}</p>
          <p>{rerunPlan.reason}</p>
          {rerunPlan.audio_reuse && <p className={`notice ${rerunPlan.audio_reuse.needs_synthesis ? 'warn' : 'info'}`} role="status">{rerunPlan.audio_reuse.unavailable ? '暂时无法核验配音缓存，执行前会再次检查。' : `原任务配音可复用 ${rerunPlan.audio_reuse.reusable_pages}/${rerunPlan.audio_reuse.total_pages} 页，需生成 ${rerunPlan.audio_reuse.needs_synthesis} 页。`}调整讲稿、音色或配音语言后，会按新设置重新核验。</p>}
          <p>将沿用原任务配置，上方新任务草稿不会用于本次续跑。</p>
          <p className="rerun-warning">将更新原任务的{rerunPlan.overwrite.join("、")}。有效配音和已完成的定位会按内容校验复用；原任务与续跑任务共用产物目录。</p>
          {rerunPlan.can_reuse_script && <p className="hint">复用已有讲稿与有效配音，继续未完成的定位、数字人或合片。若讲稿在执行前失效，任务会停止。重写讲稿请使用课件审核中的重写操作。</p>}
          {!rerunPlan.can_reuse_script && <p className="notice warn">现有讲稿无法复用，本次不会启动重写或配音。请先到课件审核中修复讲稿。</p>}
        </>}
      </Dialog>

      <footer className="footline">
        单卡串行执行 · 页面可关闭，任务在后端继续
        <br />
        <span className="est">
          {est && !courseEnabled ? (
            <>
              本地配音与合片参考{" "}
              <b>
                {est.lo}~{est.hi} 分钟
              </b>
              {"（"}
              {`按新任务目标 ${est.slides} 页估算`}
              {" · "}
              {scriptLang === "zh" ? "中文讲稿" : "英文讲稿"}
              {est.factor !== 1 ? ` · 本机硬件折算 ×${est.factor}` : ""}
              {"）"}
            </>
          ) : (
            "生成等待时间以任务实际进度为准；讲稿阶段显示动态剩余时间。"
          )}
        </span>
        {est && !courseEnabled ? (
          <span className="basis">
            {`　标定基准：${est.anchoredOn} · 实际以生成页数为准；此估算对应新任务草稿`}
          </span>
        ) : null}
      </footer>
    </div>
  );
}
