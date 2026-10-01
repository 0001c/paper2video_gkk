import { useEffect, useRef, useState } from "react";
import { Button } from "tdesign-react";
import { api, errorMessage, runExclusive } from "./flow";
import { CursorPicker, type CursorOption } from './CursorPicker';

type Review = { version: string; pages: string[]; editable: boolean;
  timing: { estimated_minutes: number; actual_minutes: number | null; target_minutes: number | null;
    pages: { planned_minutes: number; estimated_minutes: number; actual_seconds: number | null; section: string }[] } | null };

export function ReviewPanel({ jobId, apiKey, apiBase, concurrency, cursors, cursorStyle, cursorScale, onJob, onSaved }: {
  jobId: string; apiKey: string; apiBase: string; concurrency: number; onJob: (id: string) => void; onSaved: () => void;
  cursors: CursorOption[]; cursorStyle: string; cursorScale: number;
}) {
  const [review, setReview] = useState<Review | null>(null);
  const [pages, setPages] = useState<string[]>([]);
  const [selected, setSelected] = useState(0);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [accepted, setAccepted] = useState(false);
  const [success, setSuccess] = useState("");
  const [refreshPrompt, setRefreshPrompt] = useState(true);
  const [style, setStyle] = useState(cursorStyle);
  const [scale, setScale] = useState(cursorScale);
  const lock = useRef(false);
  const generation = useRef(0);
  const load = () => runExclusive(lock, setBusy, async () => {
    const current = ++generation.current;
    setError("");
    const data = await api.get(`/api/jobs/${jobId}/review`);
    if (current === generation.current) { setReview(data); setPages(data.pages); setSelected(0); setAccepted(false); }
  }, (e) => setError(errorMessage(e)));
  useEffect(() => { load(); return () => { generation.current++; }; }, [jobId]);
  const apply = (action: "save" | "render" | "regenerate") => runExclusive(lock, setBusy, async () => {
    if (!review) return;
    setError(""); setSuccess("");
    if (action === "regenerate" && !window.confirm(`重新生成第 ${selected + 1} 页讲稿会调用任务保存的讲稿模型，可能产生 API 费用。${refreshPrompt ? "使用该内容类型的最新模板替换任务旧提示词。" : "沿用任务原提示词。"}其余页会保留，是否继续？`)) return;
    const body = { version: review.version, pages };
    const { data } = action === "save" ? await api.put(`/api/jobs/${jobId}/review`, body)
      : await api.post(`/api/jobs/${jobId}/review/run`, { ...body, action, page_ids: action === "regenerate" ? [selected] : [], api_key: apiKey || undefined, api_base: apiBase || undefined, refresh_prompt: refreshPrompt, script_concurrency: concurrency, cursor_style: style, cursor_scale: scale });
    if (action === "save") { setReview(data); setPages(data.pages); setAccepted(false); setSuccess("讲稿已保存，历史版本可回溯。修改页的旧音频将在下次生成时更新。"); onSaved(); }
    else onJob(data.job_id);
  }, (e) => setError(errorMessage(e)));
  const timing = review?.timing;
  const delta = timing?.actual_minutes != null && timing.target_minutes ? (timing.actual_minutes - timing.target_minutes) / timing.target_minutes : null;
  const estimatedDelta = timing?.target_minutes ? (timing.estimated_minutes - timing.target_minutes) / timing.target_minutes : null;
  return <section className="review-panel" aria-label="课件与讲稿审核">
    <div className="section-head"><h2 className="section-title">课件与讲稿审核</h2><Button variant="text" disabled={busy} onClick={() => {
      if (pages.join('\n') !== review?.pages.join('\n') && !window.confirm('重新读取会放弃未保存的讲稿编辑，是否继续？')) return;
      load();
    }}>重新读取</Button></div>
    <p className="hint">核对课件与讲稿后再开始配音合片。可修改单页，未变化的音频和定位结果会复用；最终视频会重新合成。</p>
    {error && <div className="notice err" role="alert">{error}。若版本已变化，请先复制未保存的编辑，再重新读取。</div>}
    {success && <p className="notice ok" role="status">{success}</p>}
    {!review && !error && <p role="status">正在读取审核内容…</p>}
    {review && <>
      {timing && <div className="notice info"><span>讲稿预算约 {timing.estimated_minutes} 分钟{timing.target_minutes ? ` · 目标 ${timing.target_minutes} 分钟` : ""}{timing.actual_minutes != null ? ` · 已合成音频 ${timing.actual_minutes} 分钟` : " · 实际时长将在音频生成后显示"}{delta != null ? ` · 偏差 ${(delta * 100).toFixed(1)}%` : ""}</span></div>}
      {delta != null && Math.abs(delta) > 0.1 && <p className="notice warn">实际时长超出目标 ±10%。可调整相应页讲稿，再重新配音合片；系统不会强行放慢语音或重复内容。</p>}
      {delta == null && estimatedDelta != null && Math.abs(estimatedDelta) > 0.1 && <p className="notice warn">当前讲稿估算与目标相差 {(estimatedDelta * 100).toFixed(1)}%。请在配音前检查讲稿是否覆盖教学内容，并按需要补充讲解或调整长度；此估算仍需用实际音频校准。</p>}
      <div className="field"><label htmlFor="review-page">查看页码</label><select id="review-page" value={selected} disabled={busy} onChange={(e) => setSelected(Number(e.target.value))}>{pages.map((_, i) => <option key={i} value={i}>第 {i + 1} 页{timing?.pages[i] ? ` · ${timing.pages[i].section}` : ""}</option>)}</select></div>
      <div className="review-grid">
        <a href={`/api/jobs/${jobId}/files/slide_imgs/${selected + 1}.png?v=${review.version}`} target="_blank" rel="noreferrer"><img alt={`第 ${selected + 1} 页课件`} src={`/api/jobs/${jobId}/files/slide_imgs/${selected + 1}.png?v=${review.version}`} /></a>
        <div className="field"><label htmlFor="review-script">第 {selected + 1} 页讲稿与光标目标</label><textarea id="review-script" rows={14} maxLength={20000} disabled={!review.editable || busy} value={pages[selected] ?? ""} onChange={(e) => { setPages(pages.map((p, i) => i === selected ? e.target.value : p)); setAccepted(false); setSuccess(""); }} /><p className="hint">每行保留“讲稿 | 光标目标”，目标引用本页可见文字；没有目标写 no。页面分隔由系统维护。</p>
        {timing?.pages[selected] && <p className="hint">本页预算 {timing.pages[selected].planned_minutes.toFixed(1)} 分钟，讲稿估算 {timing.pages[selected].estimated_minutes.toFixed(1)} 分钟{timing.pages[selected].actual_seconds != null ? `，实际音频 ${(timing.pages[selected].actual_seconds! / 60).toFixed(1)} 分钟` : ""}（保存后更新估算）。</p>}
        </div>
      </div>
      <label className="checkrow"><input type="checkbox" checked={refreshPrompt} disabled={!review.editable || busy} onChange={(e) => setRefreshPrompt(e.target.checked)} />重写时使用当前内容类型的最新讲稿模板（替换任务旧提示词）</label>
      <div className="actions"><Button variant="outline" disabled={!review.editable || busy} onClick={() => apply("save")}>保存全部讲稿修改</Button><Button variant="outline" disabled={!review.editable || busy || !apiKey.trim()} onClick={() => apply("regenerate")}>调用模型重写本页</Button></div>
      <CursorPicker options={cursors} style={style} scale={scale} disabled={!review.editable || busy} onStyle={(value) => { setStyle(value); setAccepted(false); }} onScale={(value) => { setScale(value); setAccepted(false); }} />
      <p className="hint">此处选择用于下一次生成的视频；只换光标，可复用现有讲稿与配音。</p>
      <label className="checkrow"><input type="checkbox" checked={accepted} disabled={!review.editable || busy} onChange={(e) => setAccepted(e.target.checked)} />已核对所有课件和讲稿，确认开始配音与合片</label>
      <div className="actions"><Button theme="primary" loading={busy} disabled={!review.editable || !accepted || busy} onClick={() => apply("render")}>确认并生成配音视频</Button></div>
      {!review.editable && <p className="hint">有任务正在使用这份产物，暂不能编辑。结束后可重新读取。</p>}
    </>}
  </section>;
}
