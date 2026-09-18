import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Button, MessagePlugin, Radio, RadioGroup, Select } from "tdesign-react";
import { clearCredentials, loadCredentials, loadJobId, saveCredentials, saveJobId } from "./storage";

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
const api = {
  get: (p: string) => fetch(p, { cache: "no-store" }).then((r) => r.json()),
  post: (p: string, body?: unknown) =>
    fetch(p, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
    }).then(async (r) => ({ status: r.status, data: await r.json().catch(() => ({})) })),
  postForm: (p: string, form: FormData) =>
    fetch(p, { method: "POST", body: form }).then(async (r) => ({
      status: r.status,
      data: await r.json().catch(() => ({})),
    })),
};

type Opt = { value: string; label: string };

/* 阶段表由后端下发（/api/options.phases）—— 步骤条渲染它，
   总进度也由后端算（progress.overall_pct），前端**不自己拼公式**。
   两边各存一份"阶段权重"是这类进度条最容易漂的地方。 */
type Phase = { key: string; label: string; weight: number; has_pages: boolean };

type Options = {
  presets: (Opt & { slide_lang: string; script_lang: string })[];
  slide_langs: Opt[];
  script_langs: Opt[];
  paper_types: Opt[];
  accept_formats: string[];
  phases: Phase[];
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
  phase_key: string;
  phase: string;
  phase_index: number;
  phase_count: number;
  /* 阶段加权总进度（0~100） */
  overall_pct: number;
  /* 只有 has_pages 的阶段才有值，其余为 null */
  page_done: number | null;
  page_total: number | null;
  phase_elapsed_s: number | null;
  phase_expected_s: number | null;
  /* 兼容字段（后端仍下发）：旧口径的页数计数 */
  tts_done: number;
  talking_done: number;
  total_pages: number;
  tail: string[];
  stalled_seconds: number | null;
  /* 卡住由**后端按阶段**判定：数字人阶段本来就几分钟没有新记录，
     用同一个阈值会在整个阶段持续误报「疑似卡住」。 */
  stalled: boolean;
  stalled_limit_s: number;
};

type Job = {
  id: string;
  status: string;
  phase: string;
  phase_key?: string;
  exit_code?: number;
  elapsed_min?: number;
  progress?: Progress;
  queue_ahead?: number;
  /* 续跑出来的任务：值是原任务 id。产物与原任务**共享**
     （后端不复制 result 目录，只在 config.result_dir 里指过去）。 */
  resumed_from?: string;
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
function useJobStream(jobId: string | null) {
  const [job, setJob] = useState<Job | null>(null);
  const [streamState, setStreamState] = useState<StreamState>("idle");
  const attemptRef = useRef(0);
  // 终态之后**不再重连**：服务端推完终态那一帧就会正常关闭流，浏览器把它当 error →
  // 若继续重连，已完成的任务会被每 5 秒重连一次，在服务端留下一串无意义的请求。
  const terminalRef = useRef(false);

  useEffect(() => {
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
      es.onmessage = (ev) => {
        try {
          const d = JSON.parse(ev.data);
          if (d.error) return;
          if (["done", "failed", "canceled"].includes(d.status)) terminalRef.current = true;
          setJob((prev) => ({ ...(prev ?? { id: jobId }), ...d }));
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
  }, [jobId]);

  return { job, streamState };
}

/* 拖放上传区 */
function Dropzone(props: {
  label: string;
  hint: string;
  accept?: string;
  picked: string | null;
  onPick: (f: File) => void;
}) {
  const [over, setOver] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);
  return (
    <div>
      <label style={{ display: "block", fontSize: 12, color: "var(--paper-ink-soft)", marginBottom: 5 }}>
        {props.label}
      </label>
      <div
        className={"dropzone" + (over ? " over" : "")}
        onClick={() => inputRef.current?.click()}
        onDragOver={(e) => {
          e.preventDefault();
          setOver(true);
        }}
        onDragLeave={() => setOver(false)}
        onDrop={(e) => {
          e.preventDefault();
          setOver(false);
          const f = e.dataTransfer.files?.[0];
          if (f) props.onPick(f);
        }}
      >
        <div className="big">{props.picked ? "已选择" : "拖入文件，或点击选择"}</div>
        <div className="small">{props.hint}</div>
        {props.picked && <div className="picked">{props.picked}</div>}
      </div>
      <input
        ref={inputRef}
        type="file"
        accept={props.accept}
        style={{ display: "none" }}
        onChange={(e) => {
          const f = e.target.files?.[0];
          if (f) props.onPick(f);
        }}
      />
    </div>
  );
}

export default function App() {
  const [opts, setOpts] = useState<Options | null>(null);
  const [sys, setSys] = useState<SysInfo | null>(null);

  // ① 素材
  const [paperFile, setPaperFile] = useState<File | null>(null);
  const [portraitFile, setPortraitFile] = useState<File | null>(null);
  const [portraitInfo, setPortraitInfo] = useState<any | null>(null);
  const [ingest, setIngest] = useState<any>(null);
  const [ingesting, setIngesting] = useState(false);

  // ② 语言与类型
  const [preset, setPreset] = useState("zh");
  const [slideLang, setSlideLang] = useState("en");
  const [scriptLang, setScriptLang] = useState("zh");
  const [paperType, setPaperType] = useState("auto");

  // ③ API key
  // 初值从浏览器本地存储回填（storage.ts 是唯一入口）：刷新页面后不用重填。
  // 勾选框允许关掉，关掉会把本地已存的那份一并清掉（见 saveCredentials）。
  const initialCreds = useMemo(loadCredentials, []);
  const [apiKey, setApiKey] = useState(initialCreds.apiKey);
  const [apiBase, setApiBase] = useState(initialCreds.apiBase);
  const [rememberKey, setRememberKey] = useState(initialCreds.remember);
  const [keyCheck, setKeyCheck] = useState<{ ok: boolean; message: string; kind: string } | null>(null);
  const [checking, setChecking] = useState(false);

  // ④ 运行
  // jobId 也持久化：刷新页面（或误关标签）后自动回到原来那个任务 ——
  // 否则每次都要回任务列表重新点一次"查看"，长任务里这很烦。
  const [jobId, setJobId] = useState<string | null>(loadJobId());
  const [artifacts, setArtifacts] = useState<any[]>([]);
  // P1：任务记录 + 逐页素材
  const [jobs, setJobs] = useState<any[]>([]);
  const [pages, setPages] = useState<any | null>(null);
  const [rerunning, setRerunning] = useState(false);
  const { job, streamState } = useJobStream(jobId);

  useEffect(() => {
    api.get("/api/options").then(setOpts).catch(() => {});
    api.get("/api/system/info").then(setSys).catch(() => {});
  }, []);

  // 凭据变化就写回本地（含"关掉记住"时清除）。
  // 放在这里而不是 submit 里：使用者在自检时填了 key 却还没建任务就刷新，
  // 那份 key 也应该留住。
  useEffect(() => {
    saveCredentials({ apiKey, apiBase, remember: rememberKey });
  }, [apiKey, apiBase, rememberKey]);

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
    const perSlide = scriptLang === "zh" ? cal.zh_per_slide_s : cal.en_per_slide_s;
    const chars: number | undefined = ingest?.chars;
    const slides = chars
      ? Math.min(24, Math.max(6, Math.round(chars / cal.chars_per_slide)))
      : null;
    const used = slides ?? 12;                    // 还没提取时按典型长度估
    const factor = sys?.perf_factor ?? 1;
    const seconds = (used * perSlide + cal.fixed_s + cal.script_api_s) * factor;
    return {
      slides,
      lo: Math.max(1, Math.round((seconds * 0.8) / 60)),
      hi: Math.max(2, Math.round((seconds * 1.25) / 60)),
      factor,
      anchoredOn: cal.anchored_on,
    };
  }, [sys, scriptLang, ingest]);

  // 预设变化时同步两个轴 —— 但要保留"单独指定某一轴"的能力，
  // 所以这里只在预设变化的那一刻同步，之后用户手动改的轴不再被覆盖。
  useEffect(() => {
    const p = opts?.presets.find((x) => x.value === preset);
    if (p) {
      setSlideLang(p.slide_lang);
      setScriptLang(p.script_lang);
    }
  }, [preset, opts]);

  const doIngest = useCallback(async () => {
    if (!paperFile) return MessagePlugin.warning("请先选择论文文件");
    setIngesting(true);
    setIngest(null);
    const form = new FormData();
    form.append("paper", paperFile);
    const { status, data } = await api.postForm("/api/ingest", form);
    setIngesting(false);
    if (status !== 200 || !data.ok) {
      setIngest({ error: data.error || "提取失败", hint: data.hint });
      return;
    }
    setIngest(data);
  }, [paperFile]);

  const verifyKey = useCallback(async () => {
    setChecking(true);
    const { data } = await api.post("/api/key/verify", { api_key: apiKey, api_base: apiBase });
    setChecking(false);
    setKeyCheck(data);
  }, [apiKey, apiBase]);

  const submit = useCallback(async () => {
    if (!ingest || ingest.error) return MessagePlugin.warning("请先上传论文并完成提取预览");
    if (!portraitFile) return MessagePlugin.warning("请上传人像图（数字人环节必需）");
    if (!apiKey) return MessagePlugin.warning("请填写 API Key（幻灯片与讲稿生成需要）");

    // 人像走**专用接口**。
    // ⚠️ 不能复用 /api/ingest：那个接口做的是"论文 → 正文提取"，
    // 拿图片进去必然报「无法识别的文件格式」—— 原先就是这么写的，
    // 导致人像永远传不上去、任务永远建不起来，界面上只看到"上传失败"。
    const form = new FormData();
    form.append("paper", portraitFile);
    const up = await api.postForm("/api/portrait", form);
    const portraitPath = up.data?.portrait_path;
    if (!portraitPath) {
      return MessagePlugin.error(up.data?.error || "人像图上传失败");
    }
    setPortraitInfo(up.data);

    const { status, data } = await api.post("/api/jobs", {
      ingest_id: ingest.ingest_id,
      portrait_path: portraitPath,
      paper_latex_root: ingest.latex_root || undefined,
      // 原始文件路径必须带上：PDF / Word 输入时后端要靠它先转成 LaTeX 工程
      // （流水线入口要「含 main.tex 的目录」）。只有 .tex 输入才不需要。
      source_path: ingest.source_path || undefined,
      // 凭据随任务一起提交（后端只存进程内存）。
      // 不传的话后端就没有可注入子进程的 key，会退回读 .env ——
      // 本地因为 .env 有 key 所以看不出问题，换成没配 .env 的服务器就会静默失败。
      api_key: apiKey,
      api_base: apiBase || undefined,
      config: {
        stages: ["1", "2", "3"],
        slide_lang: slideLang,
        script_lang: scriptLang,
        paper_type: paperType,
        reuse_script: false,
      },
    });
    if (status !== 200) return MessagePlugin.error(data.detail || "创建任务失败");
    setJobId(data.job_id);
    setArtifacts([]);
  }, [ingest, portraitFile, apiKey, slideLang, scriptLang, paperType]);

  // 任务列表：每 5 秒刷新。本地工具，这点开销可以忽略，
  // 换来的是"后端重启前留下的任务也能看到最新状态"。
  useEffect(() => {
    const load = () =>
      api.get("/api/jobs").then((d) => setJobs(d.jobs || [])).catch(() => {});
    load();
    const timer = window.setInterval(load, 5000);
    return () => window.clearInterval(timer);
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
    if (!jobId) {
      setArtifacts([]);
      setPages(null);
      return;
    }
    let alive = true;
    const load = () => {
      api.get(`/api/jobs/${jobId}/artifacts`)
        .then((d) => alive && setArtifacts(d.artifacts || []))
        .catch(() => {});
      api.get(`/api/jobs/${jobId}/pages`)
        .then((d) => alive && setPages(d))
        .catch(() => alive && setPages(null));
    };
    load();
    let timer: number | undefined;
    if (runningNow) timer = window.setInterval(load, 5000);
    return () => {
      alive = false;
      if (timer) window.clearInterval(timer);
    };
  }, [jobId, job?.status, phaseKeyNow, runningNow]);

  // 续跑：默认只重跑 stage 2/3 且复用已有讲稿。
  // 讲稿生成是整条链路里唯一付费的一步（占约 76% token），复用即零 API 费用。
  // 凭据要一起带上：服务端把 key 挂在**任务**上（不落盘），重启服务后
  // 内存里那份就没了；而前端本地存着，随手带上最省事。
  const doRerun = useCallback(async (id: string) => {
    setRerunning(true);
    const { status, data } = await api.post(`/api/jobs/${id}/rerun`, {
      api_key: apiKey || undefined,
      api_base: apiBase || undefined,
    });
    setRerunning(false);
    if (status !== 200) {
      MessagePlugin.error(data.detail || "续跑失败");
      return;
    }
    setJobId(data.job_id);
    setArtifacts([]);
    setPages(null);
    MessagePlugin.success("已入队：只重跑 stage 2/3，复用已有讲稿（本次不产生 API 费用）");
  }, [apiKey, apiBase]);

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

  const cancel = useCallback(async () => {
    if (!jobId) return;
    await api.post(`/api/jobs/${jobId}/cancel`);
    MessagePlugin.info("已请求取消");
  }, [jobId]);

  // ── 进度：**全部由后端算**（overall_pct），前端不再自己拼 done/total ──
  // 旧公式 `talking_done || tts_done` 造成两个可见的错：幻灯片/讲稿阶段永远是 0%，
  // 数字人开工时从 100% 掉回 7%。现在用阶段加权（口径见 app.py 的 _phase_overall_pct）。
  const p = job?.progress;
  const pct = job?.status === "done" ? 100 : p?.overall_pct ?? 0;
  const phases: Phase[] = opts?.phases ?? [];
  const curIdx = p?.phase_index ?? -1;
  const stalled = !!p?.stalled;
  const pageTotal = p?.page_total ?? 0;
  const pageDone = p?.page_done ?? 0;

  // "已用 / 约"：没有逐页进度的阶段**不编造进度**，改用时间说明"它还活着"。
  const fmtMin = (s: number | null | undefined) =>
    s == null ? "" : s >= 90 ? `${(s / 60).toFixed(1)} 分钟` : `${Math.round(s)} 秒`;
  const phaseHint = useMemo(() => {
    if (!p) return "";
    const parts: string[] = [];
    if (p.phase_elapsed_s != null) parts.push(`已用 ${fmtMin(p.phase_elapsed_s)}`);
    if (p.phase_expected_s != null) parts.push(`约 ${fmtMin(p.phase_expected_s)}（粗估）`);
    if (pageTotal > 0 && phases.find((x) => x.key === p.phase_key)?.has_pages) {
      parts.push(`第 ${pageDone} / ${pageTotal} 页`);
    }
    return parts.join(" · ");
  }, [p, pageTotal, pageDone, phases]);

  const running = job && ["running", "queued"].includes(job.status);

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
        <h1>论文自动讲解视频</h1>
        <p className="sub">
          上传论文与素材，生成带数字人讲解与光标指引的演示视频
        </p>
        <div className="meta">
          <span>Paper2Video</span>
          <span>本机运行 · 127.0.0.1</span>
          <span>单卡串行</span>
        </div>

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
            <span>正在读取本机配置…</span>
          )}
        </div>
      </header>

      {/* ① 素材 */}
      <section className="section">
        <div className="section-head">
          <span className="section-num">1</span>
          <h2 className="section-title">素材</h2>
          <span className="section-note">支持 Word / PDF / LaTeX</span>
        </div>
        <div className="grid grid-2">
          <Dropzone
            label="论文文件"
            hint=".docx / .doc / .pdf / .tex / .zip"
            accept=".docx,.doc,.pdf,.tex,.zip"
            picked={paperFile?.name ?? null}
            onPick={(f) => {
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
          <div>
            <Dropzone
              label="人像图"
              hint="正面照，人脸占比大、无遮挡"
              accept="image/*"
              picked={portraitFile?.name ?? null}
              onPick={(f) => {
                setPortraitFile(f);
                setPortraitInfo(null); // 换图后旧的校验结果作废
              }}
            />
            <div className="hint" style={{ fontSize: 11.5, color: "var(--paper-ink-faint)", marginTop: 6 }}>
              参考音频可留空 —— 会按讲稿语言自动选择音色
            </div>
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
          <Button theme="primary" variant="outline" loading={ingesting} onClick={doIngest}>
            提取并预览
          </Button>
          <span style={{ fontSize: 12, color: "var(--paper-ink-faint)" }}>
            先确认提取结果，再开跑 —— 转换错了下游无法补救
          </span>
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
      </section>

      {/* ② 语言与类型 */}
      <section className="section">
        <div className="section-head">
          <span className="section-num">2</span>
          <h2 className="section-title">语言与论文类型</h2>
        </div>
        <div className="grid grid-3">
          <div className="field">
            <label>语言预设</label>
            <Select
              value={preset}
              onChange={(v) => setPreset(v as string)}
              options={(opts?.presets ?? []).map((x) => ({ label: x.label, value: x.value }))}
              style={{ width: "100%" }}
            />
            <div className="hint">预设会同时设定下面两个轴，之后可单独覆盖</div>
          </div>
          <div className="field">
            <label>幻灯片语言</label>
            <Select
              value={slideLang}
              onChange={(v) => setSlideLang(v as string)}
              options={(opts?.slide_langs ?? []).map((x) => ({ label: x.label, value: x.value }))}
              style={{ width: "100%" }}
            />
          </div>
          <div className="field">
            <label>讲稿语言</label>
            <Select
              value={scriptLang}
              onChange={(v) => setScriptLang(v as string)}
              options={(opts?.script_langs ?? []).map((x) => ({ label: x.label, value: x.value }))}
              style={{ width: "100%" }}
            />
          </div>
        </div>
        <div className="grid" style={{ marginTop: 16 }}>
          <div className="field">
            <label>论文类型</label>
            <RadioGroup value={paperType} onChange={(v) => setPaperType(v as string)}>
              {(opts?.paper_types ?? []).map((x) => (
                <Radio key={x.value} value={x.value}>
                  {x.label}
                </Radio>
              ))}
            </RadioGroup>
            <div className="hint">
              显式指定会覆盖模型的自主判断。人文社科论文若被误判成实验型，
              会凭空生成实验数据 —— 这类错误事后很难补救，建议明确指定。
            </div>
          </div>
        </div>
        <div className="notice info">
          <span>
            注意：预设「英文幻灯片 + 中文讲解」中，<b>幻灯片仍是英文</b>，只有讲稿与字幕是中文。
          </span>
        </div>
      </section>

      {/* ③ API key */}
      <section className="section">
        <div className="section-head">
          <span className="section-num">3</span>
          <h2 className="section-title">接口凭据</h2>
          <span className="section-note">
            {rememberKey ? "保存在本机浏览器" : "仅本次会话（刷新即丢）"}
          </span>
        </div>
        <div className="grid grid-2">
          <div className="field">
            <label>API Key</label>
            <input type="password" value={apiKey} onChange={(e) => setApiKey(e.target.value)} placeholder="sk-..." />
          </div>
          <div className="field">
            <label>API Base</label>
            <input type="text" value={apiBase} onChange={(e) => setApiBase(e.target.value)} placeholder="https://api.deepseek.com" />
          </div>
        </div>
        <div className="actions">
          <Button variant="outline" loading={checking} onClick={verifyKey}>连接自检</Button>
          <label className="checkline">
            <input
              type="checkbox"
              checked={rememberKey}
              onChange={(e) => setRememberKey(e.target.checked)}
            />
            记住本机（存在浏览器 localStorage，刷新后不用重填）
          </label>
          <button
            type="button"
            className="linkbtn"
            onClick={() => {
              setApiKey("");
              setApiBase("");
              clearCredentials();
              setKeyCheck(null);
              MessagePlugin.info("已清除本机保存的凭据");
            }}
          >
            清除本地凭据
          </button>
        </div>
        <div className="hint">
          提交前建议先自检 —— 余额不足时网关返回空响应，症状与网络抖动完全一样。
          <br />
          凭据只存<b>本机浏览器</b>与服务的进程内存，不写入任何文件、也不会写进任务记录；
          服务端按任务隔离，多人共用同一服务时各自用自己的 key。
          （localStorage 是同源可见的，本机/内网使用足够；将来开放公网访问需改为服务端会话。）
        </div>
        {keyCheck && (
          <div className={"notice " + (keyCheck.ok ? "ok" : "err")}>
            <span>
              <b>{keyCheck.ok ? "连接正常" : keyCheck.message}</b>
              {!keyCheck.ok && keyCheck.kind === "insufficient_balance" && (
                <>　—— 请先充值，这类失败不会给出明确错误码</>
              )}
            </span>
          </div>
        )}
      </section>

      {/* ④ 运行 */}
      <section className="section">
        <div className="section-head">
          <span className="section-num">4</span>
          <h2 className="section-title">运行</h2>
          <span className="section-note">{running ? "任务执行中" : "未运行"}</span>
        </div>

        <div className="actions">
          <Button theme="primary" disabled={!!running} onClick={submit}>
            开始生成
          </Button>
          {running && <Button variant="outline" onClick={cancel}>取消</Button>}
          {job?.queue_ahead ? (
            <span style={{ fontSize: 12, color: "var(--paper-ink-faint)" }}>
              队列中，前面还有 {job.queue_ahead} 个任务
            </span>
          ) : null}
        </div>

        {job && (
          <div style={{ marginTop: 18 }}>
            <div className="statusline">
              <span className={"dot " + (job.status === "done" ? "ok" : job.status === "failed" ? "err" : "run")} />
              <span>{job.phase || job.status}</span>
              <span className="tail">
                {`总进度 ${pct}%`}
                {phaseHint ? `　${phaseHint}` : ""}
                {job.elapsed_min ? `　已跑 ${job.elapsed_min} min` : ""}
                {job.exit_code !== undefined && job.exit_code !== 0 ? `　退出码 ${job.exit_code}` : ""}
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

            {/* 逐页格子只在"有页数概念"的阶段显示。
                原先它一直挂着，幻灯片/讲稿/合片阶段是一排永远不动的空点。 */}
            {pageTotal > 0 && (
              <div className="pips">
                {Array.from({ length: pageTotal }).map((_, i) => (
                  <span key={i} className={i < pageDone ? "done" : i === pageDone ? "cur" : "todo"} />
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

            {p?.tail?.length ? <div className="logtail">{p.tail.join("\n")}</div> : null}
          </div>
        )}

        {/* 产物区**运行中也渲染**。原先外层是 `artifacts.length > 0 &&`，
            于是整块在运行期间不存在，"跑完才冒出来"看起来像丢了产物。 */}
        {job && (
          <div style={{ marginTop: 20 }}>
            <div className="statusline" style={{ marginBottom: 4 }}>
              <span className={"dot " + (artifacts.length ? (job.status === "done" ? "ok" : "run") : "wait")} />
              <span>产物</span>
              <span className="tail">
                {artifacts.length ? `${artifacts.length} 项` : "尚未生成"}
                {running && artifacts.length ? "　（每 5 秒刷新）" : ""}
              </span>
              {job.resumed_from && (
                <span className="section-note" style={{ marginLeft: 8 }}>
                  续跑自 {job.resumed_from} · 与原任务共享同一份产物目录（重跑会覆盖它）
                </span>
              )}
            </div>
            {artifacts.length > 0 ? (
              <div className="artifacts">
                {artifacts.map((a) => (
                  <a key={a.name} href={`/api/jobs/${jobId}/files/${encodeURIComponent(a.name)}`} target="_blank" rel="noreferrer">
                    <span>{a.name}</span>
                    <span className="sz">{a.size_mb} MB</span>
                  </a>
                ))}
              </div>
            ) : (
              <div className="hint">
                产物会在各阶段结束后陆续出现在这里（运行中也能看到已生成的部分）。
              </div>
            )}
            {artifacts.some((a) => a.name === "3_merage.mp4") && (
              <video controls src={`/api/jobs/${jobId}/files/3_merage.mp4`} />
            )}
          </div>
        )}

        {/* P1 逐页素材浏览：服务端已把命名规则不一致的产物索引到一起
            （幻灯片 1.png 起、音频 0.wav 起、数字人还多一层参考图目录）。 */}
        {pages?.total > 0 && (
          <div style={{ marginTop: 22 }}>
            <div className="statusline" style={{ marginBottom: 6 }}>
              <span className="dot ok" />
              <span>逐页素材</span>
              <span className="tail">共 {pages.total} 页 · 点图看大图</span>
            </div>
            {pages.slides_pdf || pages.script_txt || pages.cursor_json ? (
              <div className="filerefs">
                {pages.slides_pdf && (
                  <a href={fileUrl(pages.slides_pdf)} target="_blank" rel="noreferrer">
                    幻灯片总览 PDF
                  </a>
                )}
                {pages.script_txt && (
                  <a href={fileUrl(pages.script_txt)} target="_blank" rel="noreferrer">
                    讲稿（含光标短语）
                  </a>
                )}
                {pages.cursor_json && (
                  <a href={fileUrl(pages.cursor_json)} target="_blank" rel="noreferrer">
                    光标 JSON
                  </a>
                )}
              </div>
            ) : null}
            <div className="pagegrid">
              {pages.pages.map((p: any) => (
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
          </div>
        )}
      </section>

      {/* ⑤ 任务记录（P1）：续跑是省钱主力，界面上不做就浪费了 */}
      <section className="section">
        <div className="section-head">
          <span className="section-num">5</span>
          <h2 className="section-title">任务记录</h2>
          <span className="section-note">续跑只重跑 stage 2/3，复用已有讲稿</span>
        </div>
        {jobs.length === 0 ? (
          <div className="hint">还没有任务。</div>
        ) : (
          <div className="jobtable">
            {jobs.map((j) => (
              <div className={"jobrow" + (j.id === jobId ? " active" : "")} key={j.id}>
                <span className={"dot " + dotOf(j.status)} />
                <span className="jid">{j.id}</span>
                <span className="jphase">
                  {j.phase}
                  {j.elapsed_min ? ` · ${j.elapsed_min} min` : ""}
                </span>
                <span className="jcfg">
                  {j.config?.slide_lang}/{j.config?.script_lang}
                  {j.config?.stages ? ` · stage ${j.config.stages.join("")}` : ""}
                  {j.config?.reuse_script ? " · 复用讲稿" : ""}
                  {j.resumed_from ? " · 续跑" : ""}
                </span>
                <span className="jactions">
                  <button type="button" className="linkbtn" onClick={() => setJobId(j.id)}>
                    查看
                  </button>
                  {["done", "failed", "canceled"].includes(j.status) && (
                    <button
                      type="button"
                      className="linkbtn"
                      disabled={rerunning}
                      onClick={() => doRerun(j.id)}
                    >
                      续跑 2/3
                    </button>
                  )}
                </span>
              </div>
            ))}
          </div>
        )}
      </section>

      <footer className="footline">
        单卡串行执行 · 页面可关闭，任务在后端继续
        <br />
        <span className="est">
          {est ? (
            <>
              本篇预计{" "}
              <b>
                {est.lo}~{est.hi} 分钟
              </b>
              {"（"}
              {est.slides ? `${est.slides} 页粗估` : "按典型篇幅 12 页估"}
              {" · "}
              {scriptLang === "zh" ? "中文讲稿" : "英文讲稿"}
              {est.factor !== 1 ? ` · 本机硬件折算 ×${est.factor}` : ""}
              {"）"}
            </>
          ) : (
            "一次生成约 15~25 分钟"
          )}
        </span>
        {est ? (
          <span className="basis">
            {`　标定基准：${est.anchoredOn} · 页数由正文字数推算，实际以 stage 1 生成的幻灯片数为准`}
          </span>
        ) : null}
      </footer>
    </div>
  );
}
