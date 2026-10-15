import { useEffect, useRef, useState } from "react";
import { Button } from "tdesign-react";
import { api, errorMessage, runExclusive } from "./flow";
import { taskModelPayload, type ModelConfig } from "./modelConfig";
import { CursorPicker, type CursorOption } from './CursorPicker';
import { Help, Section } from './Section';
import type { SubtitleStyle } from './subtitleStyle';

type Review = { version: string; pages: string[]; editable: boolean;
  oral_previews?: (string | null)[];
  teaching_tasks?: { title: string; role: string; goal: string; handoff_question: string | null }[];
  teaching?: { passed: boolean; issues: string[]; warnings: string[];
    lesson_ledger?: { pages: { page: number; question_id: string; question_status: string;
      knowledge: { id: string; text: string; status: string; evidence: string | null }[] }[];
      pending_questions: { id: string; asked_page: number; answer_page: number | null; quote: string; status: string }[] } | null;
    tensor_contracts?: { example_id: string; source_page: number | null; symbols: Record<string, number[]> }[];
    example_coverage: { example_id: string; source_page: number | null; slide_pages: number[] }[] } | null;
  timing: { estimated_minutes: number; actual_minutes: number | null; target_minutes: number | null;
    configured_speech_rate?: number; estimate_speech_rate?: number;
    pages: { planned_minutes: number; estimated_minutes: number; actual_seconds: number | null; section: string }[] } | null };

export function ReviewPanel({ jobId, apiKey, apiBase, modelConfig, modelOrigin, voice, subtitleStyle, concurrency, cursors, cursorStyle, cursorScale, onJob, onSaved }: {
  jobId: string; apiKey: string; apiBase: string; concurrency: number; onJob: (id: string) => void; onSaved: () => void;
  cursors: CursorOption[]; cursorStyle: string; cursorScale: number;
  modelConfig?: ModelConfig; modelOrigin?: string; voice?: unknown;
  subtitleStyle: SubtitleStyle;
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
      : await api.post(`/api/jobs/${jobId}/review/run`, { ...body, action, page_ids: action === "regenerate" ? [selected] : [],
        ...taskModelPayload({ model_config: modelConfig, model_config_origin: modelOrigin, voice }, apiKey, apiBase),
        refresh_prompt: refreshPrompt, script_concurrency: concurrency, cursor_style: style, cursor_scale: scale, subtitle_style: subtitleStyle,
        qa_override: action === 'render' && !!modelConfig?.evaluation?.enabled && accepted });
    if (action === "save") { setReview(data); setPages(data.pages); setAccepted(false); setSuccess("讲稿已保存，历史版本可回溯。修改页的旧音频将在下次生成时更新。"); onSaved(); }
    else onJob(data.job_id);
  }, (e) => setError(errorMessage(e)));
  const timing = review?.timing;
  const task = review?.teaching_tasks?.[selected];
  const ledger = review?.teaching?.lesson_ledger;
  const checklist = ledger?.pages.find((page) => page.page === selected + 1);
  const teachingNeedsSave = !!review?.teaching && pages.join('\n') !== review.pages.join('\n');
  const delta = timing?.actual_minutes != null && timing.target_minutes ? (timing.actual_minutes - timing.target_minutes) / timing.target_minutes : null;
  const estimatedDelta = timing?.target_minutes ? (timing.estimated_minutes - timing.target_minutes) / timing.target_minutes : null;
  return <Section title="课件与讲稿审核" important help="核对课件与讲稿后再确认配音合片。可逐页编辑；未变化的音频与定位结果可复用，最终视频会重新合成。光标选择用于下一次生成，已有成片不会自动更改。" actions={<Button variant="text" disabled={busy} onClick={() => {
      if (pages.join('\n') !== review?.pages.join('\n') && !window.confirm('重新读取会放弃未保存的讲稿编辑，是否继续？')) return;
      load();
    }}>重新读取</Button>}>
    {error && <div className="notice err" role="alert">{error}。若版本已变化，请先复制未保存的编辑，再重新读取。</div>}
    {success && <p className="notice ok" role="status">{success}</p>}
    {!review && !error && <p role="status">正在读取审核内容…</p>}
    {review && <>
      {timing && <div className="notice info"><span>讲稿预算约 {timing.estimated_minutes} 分钟{timing.target_minutes ? ` · 目标 ${timing.target_minutes} 分钟` : ""}{timing.actual_minutes != null ? ` · 已合成音频 ${timing.actual_minutes} 分钟` : " · 实际时长将在音频生成后显示"}{delta != null ? ` · 偏差 ${(delta * 100).toFixed(1)}%` : ""}</span></div>}
      {timing?.estimate_speech_rate != null && timing.configured_speech_rate != null && timing.estimate_speech_rate !== timing.configured_speech_rate && <p className="hint">时长估算已按本课音色实测语速校准为每分钟约 {timing.estimate_speech_rate} 字词。</p>}
      {review.teaching && <div aria-live="polite">
        {review.teaching.issues.map((issue) => <p className="notice warn" key={issue}>{issue}</p>)}
        {review.teaching.warnings.map((warning) => <p className="hint" key={warning}>{warning}</p>)}
        {review.teaching.example_coverage.map((example) => <p className="hint" key={example.example_id}>例题 {example.example_id} · 源文{example.source_page ? `第 ${example.source_page} 页` : "页码未标记"} · {example.slide_pages.length ? `课件第 ${example.slide_pages.join("、")} 页` : "待补充课件步骤"}</p>)}
      </div>}
      {delta != null && Math.abs(delta) > 0.1 && <p className="notice warn">实际时长超出目标 ±10%。可调整相应页讲稿，再重新配音合片；系统不会强行放慢语音或重复内容。</p>}
      {delta == null && estimatedDelta != null && Math.abs(estimatedDelta) > 0.1 && <p className="notice warn">当前讲稿估算与目标相差 {(estimatedDelta * 100).toFixed(1)}%。请在配音前检查讲稿是否覆盖教学内容，并按需要补充讲解或调整长度；此估算仍需用实际音频校准。</p>}
      <div className="field"><label htmlFor="review-page">查看页码</label><select id="review-page" value={selected} disabled={busy} onChange={(e) => setSelected(Number(e.target.value))}>{pages.map((_, i) => <option key={i} value={i}>第 {i + 1} 页{timing?.pages[i] ? ` · ${timing.pages[i].section}` : ""}</option>)}</select></div>
      <div className="review-grid">
        <a href={`/api/jobs/${jobId}/files/slide_imgs/${selected + 1}.png?v=${review.version}`} target="_blank" rel="noreferrer"><img alt={`第 ${selected + 1} 页课件`} src={`/api/jobs/${jobId}/files/slide_imgs/${selected + 1}.png?v=${review.version}`} /></a>
        <div className="field"><div className="field-title"><label htmlFor="review-script">第 {selected + 1} 页讲稿与光标目标</label><Help title="讲稿输出格式" important>每行保留“讲稿 | 光标目标”，目标引用本页可见文字；没有目标写 no。页面分隔由系统维护。</Help></div><textarea id="review-script" rows={14} maxLength={20000} disabled={!review.editable || busy} value={pages[selected] ?? ""} onChange={(e) => { setPages(pages.map((p, i) => i === selected ? e.target.value : p)); setAccepted(false); setSuccess(""); }} />
        {task && <p className="hint">讲解任务 · {task.role}：{task.goal}。{task.handoff_question ? `下一页承接问题：${task.handoff_question}` : "本页收束全课问题。"}</p>}
        {checklist && <details><summary>本页教学核对清单 · {checklist.question_id} {checklist.question_status}</summary>
          <p className="hint">前文最多一句承接。原文出现只表示有证据，需核对是否解释清楚；转述与跨页回答请人工确认。</p>
          <ul>{checklist.knowledge.map((fact) => <li key={fact.id}>{fact.id}：{fact.text} · {fact.status}</li>)}</ul>
          {ledger?.pending_questions.filter((q) => q.asked_page === selected + 1 || q.answer_page === selected + 1).map((q) => <p className="hint" key={q.id}>第 {q.asked_page} 页提出：{q.quote} · {q.answer_page ? `第 ${q.answer_page} 页负责` : '待指定负责页'} · {q.status}</p>)}
        </details>}
        {!!review.teaching?.tensor_contracts?.length && <details><summary>整课符号与形状约束</summary>
          {review.teaching.tensor_contracts.map((c) => <p className="hint" key={c.example_id}>例题 {c.example_id} · 源文第 {c.source_page ?? '未标记'} 页：{Object.entries(c.symbols).map(([name, dims]) => `${name} [${dims.join(', ')}]`).join('；')}</p>)}
        </details>}
        {review.oral_previews && <details><summary>实际口播预览（保存后更新）</summary><p>{review.oral_previews[selected] ?? '含不支持的公式，请先改为明确口述。'}</p><p className="hint">代码中的点、下划线会明确读出；展示原文和光标引用保持原格式，云时间戳仍逐字校验。</p></details>}
        {timing?.pages[selected] && <p className="hint">本页预算 {timing.pages[selected].planned_minutes.toFixed(1)} 分钟，讲稿估算 {timing.pages[selected].estimated_minutes.toFixed(1)} 分钟{timing.pages[selected].actual_seconds != null ? `，实际音频 ${(timing.pages[selected].actual_seconds! / 60).toFixed(1)} 分钟` : ""}（保存后更新估算）。</p>}
        </div>
      </div>
      <label className="checkrow"><input type="checkbox" checked={refreshPrompt} disabled={!review.editable || busy} onChange={(e) => setRefreshPrompt(e.target.checked)} />重写时使用当前内容类型的最新讲稿模板（替换任务旧提示词）</label>
      <div className="actions"><Button variant="outline" disabled={!review.editable || busy} onClick={() => apply("save")}>保存全部讲稿修改</Button><Button variant="outline" disabled={!review.editable || busy || (modelOrigin !== 'v2' && !apiKey.trim())} onClick={() => apply("regenerate")}>调用模型重写本页</Button></div>
      <CursorPicker options={cursors} style={style} scale={scale} disabled={!review.editable || busy} onStyle={(value) => { setStyle(value); setAccepted(false); }} onScale={(value) => { setScale(value); setAccepted(false); }} />
      <label className="checkrow"><input type="checkbox" checked={accepted} disabled={!review.editable || busy} onChange={(e) => setAccepted(e.target.checked)} />{modelConfig?.evaluation?.enabled ? '已核对全部内容，手动确认继续（问答未通过时也继续）' : '已核对所有课件和讲稿，确认开始配音与合片'}</label>
      {teachingNeedsSave && <p className="hint">讲稿已修改，请先保存，重新核验例题与时长预算。</p>}
      <div className="actions"><Button theme="primary" loading={busy} disabled={!review.editable || !accepted || busy || teachingNeedsSave || review.teaching?.passed === false} onClick={() => apply("render")}>确认并生成配音视频</Button></div>
      {!review.editable && <p className="hint">有任务正在使用这份产物，暂不能编辑。结束后可重新读取。</p>}
    </>}
  </Section>;
}
