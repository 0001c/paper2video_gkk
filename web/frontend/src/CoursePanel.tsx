import { useEffect, useRef, useState } from "react";
import { Button } from "tdesign-react";
import { api, errorMessage, runExclusive } from "./flow";
import { Help, Section } from './Section';

export type CourseSettings = {
  section_ids: string[]; duration_mode: "auto" | "custom"; target_minutes: number;
  speech_rate: number; audience: string; objectives: string; focus: string; depth: string;
  sections?: { title: string; minutes: number }[];
};
export const defaultCourse: CourseSettings = {
  section_ids: ["all"], duration_mode: "auto", target_minutes: 40, speech_rate: 220,
  audience: "", objectives: "", focus: "", depth: "循序渐进，解释概念并讲清例题和推导",
};
export type CoursePlan = {
  token: string; planned_minutes: number; target_minutes: number | null; warnings: string[];
  sections: { id: string; title: string; minutes: number; first_page: number; last_page: number }[];
};

export function CoursePanel({ ingestId, settings, onSettings, slideCount, scriptLang, onPlan }: {
  ingestId?: string; settings: CourseSettings; onSettings: (v: CourseSettings) => void;
  slideCount: number | ""; scriptLang: string; onPlan: (v: CoursePlan | null) => void;
}) {
  const [source, setSource] = useState<{ id: string; title: string; chars: number }[]>([]);
  const [sourceError, setSourceError] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [draftPlan, setDraftPlan] = useState<CoursePlan | null>(null);
  const [confirmed, setConfirmed] = useState(false);
  const [planContext, setPlanContext] = useState("");
  const lock = useRef(false);
  const context = JSON.stringify({ ingestId, settings, slideCount, scriptLang });
  const contextRef = useRef(context);
  contextRef.current = context;
  useEffect(() => { onPlan(null); setConfirmed(false); }, [context, onPlan]);
  useEffect(() => {
    let alive = true;
    setSource([]); setSourceError(""); setDraftPlan(null);
    if (!ingestId) return;
    api.get(`/api/course/source/${encodeURIComponent(ingestId)}`).then((d) => {
      if (alive) setSource(d.sections);
    }).catch((e) => { if (alive) setSourceError(errorMessage(e)); });
    return () => { alive = false; };
  }, [ingestId]);
  const update = (value: Partial<CourseSettings>) => onSettings({ ...settings, ...value });
  const prepare = () => runExclusive(lock, setBusy, async () => {
    const current = contextRef.current;
    setError("");
    const { data } = await api.post("/api/course/plan", { ingest_id: ingestId,
      settings, slide_count: slideCount, script_lang: scriptLang });
    if (current === contextRef.current) { setDraftPlan(data); setPlanContext(current); setConfirmed(false); }
  }, (e) => setError(errorMessage(e)));
  return <Section anchor="teaching" number="04" title="教学范围与目标时长" defaultOpen={false} label="课程计划" summary={confirmed ? `已确认 · ${draftPlan?.planned_minutes} 分钟` : '待确认教案'} important help={<>
    <p>先提取素材，再选择章节或小节。选整章会包含其小节，请避免重复选择；没有可靠标题时可以上传单独的小节文件。</p>
    <p>预览教案在本机分配教学时间，不调用生成接口。核对后再生成课件与讲稿；修改范围或时间后需重新更新并确认。</p>
    <p>目标时长用于讲稿预算，合成后显示实际时长及偏差。不保证精确到秒，建议验收范围 ±10%。</p>
  </>}>
    {!ingestId && <p className="notice info" role="status">请先提取素材，再选择教学范围。</p>}
    {sourceError && <p role="alert" className="notice err">{sourceError}</p>}
    {source.length > 0 && <Section title="选择教学范围" compact defaultOpen={false} summary={`已选 ${settings.section_ids.length} 项`}>
      <div className="course-sources">{source.map((s) => <label className="checkrow" key={s.id}>
        <input type="checkbox" checked={settings.section_ids.includes(s.id)} onChange={(e) => {
          const ids = s.id === "all" ? (e.target.checked ? ["all"] : []) : e.target.checked
            ? [...settings.section_ids.filter((id) => id !== "all"), s.id] : settings.section_ids.filter((id) => id !== s.id);
          update({ section_ids: ids, sections: undefined });
        }} />{s.title} <span className="hint">{s.chars} 字符</span>
      </label>)}</div>
    </Section>}
    <div className="grid grid-2 course-fields">
      <div className="field"><label htmlFor="course-mode">成片播放时长</label><select id="course-mode" value={settings.duration_mode} onChange={(e) => update({ duration_mode: e.target.value as "auto" | "custom", sections: undefined })}>
        <option value="auto">自动分配（审核教案时可调整）</option><option value="custom">自定义目标分钟数</option>
      </select></div>
      {settings.duration_mode === "custom" && <div className="field"><label htmlFor="course-target">目标分钟数（1～180）</label>
        <input id="course-target" type="number" min="1" max="180" step="0.5" value={settings.target_minutes} onChange={(e) => update({ target_minutes: Number(e.target.value), sections: undefined })} /></div>}
      <div className="field"><div className="field-title"><label htmlFor="course-rate">预算语速（{scriptLang === "zh" ? "字" : "词"}/分钟）</label><Help title="预算语速">用于讲稿长度预算；不会改变音色、语速或强行拉伸音频。最终时长需要实际配音后校准。</Help></div><input id="course-rate" type="number" min="80" max="400" value={settings.speech_rate} onChange={(e) => update({ speech_rate: Number(e.target.value) })} /></div>
      <div className="field"><label htmlFor="course-audience">学生基础 / 受众</label><input id="course-audience" maxLength={2000} placeholder="例如：大一学生，已学习集合和基本代数" value={settings.audience} onChange={(e) => update({ audience: e.target.value })} /></div>
      {([['objectives', '学习目标', '学完后学生应该能解释或完成什么'], ['focus', '重点与难点', '例如：公式推导和例题中容易混淆的步骤'], ['depth', '讲解深度与方式', '概念解释、推导、例题、易错点、小结']] as const).map(([key, label, hint]) => <div className="field" key={key}><label htmlFor={`course-${key}`}>{label}</label><textarea id={`course-${key}`} maxLength={2000} rows={3} placeholder={hint} value={settings[key]} onChange={(e) => update({ [key]: e.target.value })} /></div>)}
    </div>
    <div className="actions"><Button variant="outline" loading={busy} disabled={!ingestId || !settings.section_ids.length} onClick={prepare}>预览 / 更新教案</Button></div>
    {error && <p className="notice err" role="alert">{error}</p>}
    {draftPlan && <div className="course-plan">
      <p>计划成片约 {draftPlan.planned_minutes} 分钟。请核对标题和分节时间；修改后点击“更新教案”。</p>
      <div className="course-table"><table><thead><tr><th>教学小节</th><th>分钟</th><th>目标页范围</th></tr></thead><tbody>{draftPlan.sections.map((s, i) => <tr key={s.id}>
        <td><input aria-label={`小节 ${i + 1} 标题`} maxLength={120} value={settings.sections?.[i]?.title ?? s.title} onChange={(e) => update({ sections: draftPlan.sections.map((r, j) => ({ title: j === i ? e.target.value : settings.sections?.[j]?.title ?? r.title, minutes: settings.sections?.[j]?.minutes ?? r.minutes })) })} /></td>
        <td><input aria-label={`小节 ${i + 1} 分钟`} type="number" min="0.1" max="180" step="0.1" value={settings.sections?.[i]?.minutes ?? s.minutes} onChange={(e) => update({ sections: draftPlan.sections.map((r, j) => ({ title: settings.sections?.[j]?.title ?? r.title, minutes: j === i ? Number(e.target.value) : settings.sections?.[j]?.minutes ?? r.minutes })) })} /></td>
        <td>{s.first_page}～{s.last_page}</td>
      </tr>)}</tbody></table></div>
      {draftPlan.warnings.map((w) => <p key={w} className="notice warn">{w}</p>)}
      <label className="checkrow"><input type="checkbox" disabled={planContext !== context} checked={confirmed} onChange={(e) => { setConfirmed(e.target.checked); onPlan(e.target.checked ? draftPlan : null); }} />已核对教学范围和时间分配{planContext !== context ? "（设置已变化，请先更新教案）" : ""}</label>
    </div>}
  </Section>;
}
