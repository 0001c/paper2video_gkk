import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Button, MessagePlugin, Progress, Radio, RadioGroup, Select } from "tdesign-react";

/* ─────────────────────────────────────────────────────────────
   接口层。两个注意点：
   1. 所有请求都是同源相对路径 —— 生产环境由 FastAPI 直接服务静态文件，
      开发环境走 vite 的 /api 代理。这样前端不需要知道后端地址。
   2. 本机有 TUN 代理（HTTP_PROXY=127.0.0.1:13960），浏览器对 localhost
      通常会自动绕过，但若出现 502，先怀疑代理（见 README）。
   ───────────────────────────────────────────────────────────── */
const api = {
  get: (p: string) => fetch(p).then((r) => r.json()),
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

type Options = {
  presets: (Opt & { slide_lang: string; script_lang: string })[];
  slide_langs: Opt[];
  script_langs: Opt[];
  paper_types: Opt[];
  accept_formats: string[];
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

type Progress = {
  tts_done: number;
  talking_done: number;
  total_pages: number;
  phase: string;
  tail: string[];
  stalled_seconds: number | null;
};

type Job = {
  id: string;
  status: string;
  phase: string;
  exit_code?: number;
  elapsed_min?: number;
  progress?: Progress;
  queue_ahead?: number;
};

/* SSE 进度流封装成一个 hook。
   为什么单独抽出来：进度是全应用唯一「持续变化」的状态，
   集中在一处比散在各组件里更好维护（也方便做重连）。 */
function useJobStream(jobId: string | null) {
  const [job, setJob] = useState<Job | null>(null);
  useEffect(() => {
    if (!jobId) {
      setJob(null);
      return;
    }
    const es = new EventSource(`/api/jobs/${jobId}/events`);
    es.onmessage = (ev) => {
      try {
        const d = JSON.parse(ev.data);
        if (d.error) return;
        setJob((prev) => ({ ...(prev ?? { id: jobId }), ...d }));
      } catch {
        /* 单条解析失败不该中断整个流 */
      }
    };
    es.onerror = () => es.close();
    return () => es.close();
  }, [jobId]);
  return job;
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
  const [ingest, setIngest] = useState<any>(null);
  const [ingesting, setIngesting] = useState(false);

  // ② 语言与类型
  const [preset, setPreset] = useState("zh");
  const [slideLang, setSlideLang] = useState("en");
  const [scriptLang, setScriptLang] = useState("zh");
  const [paperType, setPaperType] = useState("auto");

  // ③ API key
  const [apiKey, setApiKey] = useState("");
  const [apiBase, setApiBase] = useState("");
  const [keyCheck, setKeyCheck] = useState<{ ok: boolean; message: string; kind: string } | null>(null);
  const [checking, setChecking] = useState(false);

  // ④ 运行
  const [jobId, setJobId] = useState<string | null>(null);
  const [artifacts, setArtifacts] = useState<any[]>([]);
  const job = useJobStream(jobId);

  useEffect(() => {
    api.get("/api/options").then(setOpts).catch(() => {});
    api.get("/api/system/info").then(setSys).catch(() => {});
  }, []);

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

    // 人像图先落到后端：/api/ingest 只处理论文，人像单独上传
    const form = new FormData();
    form.append("paper", portraitFile);
    const up = await api.postForm("/api/ingest", form);   // 复用同一落盘逻辑
    const portraitPath = up.data?.source_path;
    if (!portraitPath) return MessagePlugin.error("人像图上传失败");

    const { status, data } = await api.post("/api/jobs", {
      ingest_id: ingest.ingest_id,
      portrait_path: portraitPath,
      paper_latex_root: ingest.latex_root || undefined,
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

  useEffect(() => {
    if (!jobId || !job || !["done", "failed"].includes(job.status)) return;
    api.get(`/api/jobs/${jobId}/artifacts`).then((d) => setArtifacts(d.artifacts || []));
  }, [jobId, job?.status]);

  const cancel = useCallback(async () => {
    if (!jobId) return;
    await api.post(`/api/jobs/${jobId}/cancel`);
    MessagePlugin.info("已请求取消");
  }, [jobId]);

  const p = job?.progress;
  const total = p?.total_pages || 0;
  const done = p?.talking_done || p?.tts_done || 0;
  const pct = total ? Math.min(100, Math.round((done / total) * 100)) : 0;
  const stalled = (p?.stalled_seconds ?? 0) > 180;

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
              onPick={setPortraitFile}
            />
            <div className="hint" style={{ fontSize: 11.5, color: "var(--paper-ink-faint)", marginTop: 6 }}>
              参考音频可留空 —— 会按讲稿语言自动选择音色
            </div>
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
          <span className="section-note">仅存内存，不落盘</span>
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
          <span style={{ fontSize: 12, color: "var(--paper-ink-faint)" }}>
            提交前建议先自检 —— 余额不足时网关返回空响应，症状与网络抖动完全一样
          </span>
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
          <>
            <div style={{ marginTop: 18 }}>
              <div className="statusline">
                <span className={"dot " + (job.status === "done" ? "ok" : job.status === "failed" ? "err" : "run")} />
                <span>{job.phase || job.status}</span>
                <span className="tail">
                  {total ? `${done} / ${total} 页` : ""}
                  {job.elapsed_min ? `　${job.elapsed_min} min` : ""}
                  {job.exit_code !== undefined && job.exit_code !== 0 ? `　退出码 ${job.exit_code}` : ""}
                </span>
              </div>

              <div className="bar">
                <i style={{ width: `${pct}%` }} />
              </div>

              {total > 0 && (
                <div className="pips">
                  {Array.from({ length: total }).map((_, i) => (
                    <span key={i} className={i < done ? "done" : i === done ? "cur" : "todo"} />
                  ))}
                </div>
              )}

              {stalled && running && (
                <div className="notice warn" style={{ marginTop: 12 }}>
                  <span>
                    <b>疑似卡住</b>　日志已有 {Math.round((p?.stalled_seconds ?? 0) / 60)} 分钟无更新。
                    本机单卡执行时单页可能要数十秒；若持续不更新可取消后重跑。
                  </span>
                </div>
              )}

              {p?.tail?.length ? <div className="logtail">{p.tail.join("\n")}</div> : null}
            </div>
          </>
        )}

        {artifacts.length > 0 && (
          <div style={{ marginTop: 20 }}>
            <div className="statusline" style={{ marginBottom: 4 }}>
              <span className={"dot " + (job?.status === "done" ? "ok" : "err")} />
              <span>产物</span>
            </div>
            <div className="artifacts">
              {artifacts.map((a) => (
                <a key={a.name} href={`/api/jobs/${jobId}/files/${encodeURIComponent(a.name)}`} target="_blank" rel="noreferrer">
                  <span>{a.name}</span>
                  <span className="sz">{a.size_mb} MB</span>
                </a>
              ))}
            </div>
            {artifacts.some((a) => a.name === "3_merage.mp4") && (
              <video controls src={`/api/jobs/${jobId}/files/3_merage.mp4`} />
            )}
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
