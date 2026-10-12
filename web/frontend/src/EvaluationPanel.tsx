import { useRef, useState } from 'react';
import { Button, Dialog } from 'tdesign-react';
import { api, errorMessage, runExclusive } from './flow';
import { Help, Section } from './Section';
import { defaultEvaluation, evaluationChecks, evaluationTargets, evaluationModelProblem, type EvaluationTarget, type ModelConfig } from './modelConfig';
import { answerResult } from './evaluationResult';
import type { SubtitleStyle } from './subtitleStyle';

type Issue = { page: number; timestamp: number; criterion: string; description: string; evidence: string; requires_review?: boolean; script_quote?: string; asr_quote?: string };
type CheckReport = { label: string; score: number; threshold: number; passed: boolean; mean_score?: number; issues?: Issue[]; pages?: { page: number; score: number; start: number; end: number; issues?: Issue[] }[] };
export type EvaluationReport = { stage: 'script' | 'video'; score: number; correct: number; total: number; fingerprint: string;
  model: string; quiz_model?: string; independent_models?: boolean; source_policy_version?: number; grading_version?: number; answer_correct?: number; sampled_frames: number;
  evaluation_version?: number; checks?: Partial<Record<EvaluationTarget, CheckReport>>; check_selection?: Partial<Record<EvaluationTarget, { enabled: boolean; threshold: number }>>;
  model_selection?: ModelConfig['stages']['qa']; quiz_model_selection?: ModelConfig['stages']['quiz']; evaluated_pages?: number;
  items: { id: string; question: string; answer: string; predicted: string | null; correct: boolean; supported: boolean; evidence: string;
    verified_evidence?: string[]; evidence_status?: string; source_quote: string; options: Record<string, string>; kind: string; timestamp?: number }[] };
type Action = 'continue' | 'optimize_script' | 'regenerate' | 'repair_slides' | 'repair_audio' | 'repair_presentation' | 'accept';
const actionLabels: Record<Action, string> = { continue: '手动继续配音合片', optimize_script: '根据评估优化讲稿', regenerate: '按知识反馈重写讲稿',
  repair_slides: '优化课件与讲稿', repair_audio: '复核后重新配音', repair_presentation: '按当前设置重新合片', accept: '接受当前成片' };
const modelIdentity = (stage?: ModelConfig['stages']['qa']) => stage && JSON.stringify([stage.mode, stage.provider, stage.model, stage.connection_id ?? '',
  Object.entries(stage.parameters ?? {}).sort(([a], [b]) => a.localeCompare(b))]);

export function EvaluationPanel({ jobId, evaluation, error, decision, nextJobId, attempt = 0, value, subtitleStyle, busy: running, hasScript, hasVideo, onJob }: {
  jobId: string; evaluation?: { script?: EvaluationReport; video?: EvaluationReport }; error?: string; decision?: string;
  nextJobId?: string; attempt?: number; value: ModelConfig; subtitleStyle?: SubtitleStyle; busy: boolean; hasScript: boolean; hasVideo: boolean; onJob: (id: string) => void;
}) {
  const policy = value.evaluation ?? { ...defaultEvaluation, enabled: false };
  const selected = evaluationChecks(policy);
  const selectedFor = (stage: 'script' | 'video') => (Object.keys(evaluationTargets) as EvaluationTarget[]).filter(name => selected[name].enabled && evaluationTargets[name].stage === stage);
  const current = (report?: EvaluationReport) => {
    if (!report || report.source_policy_version !== 2 || report.grading_version !== 2) return false;
    if (policy.checks) {
      const names = selectedFor(report.stage);
      if (report.evaluation_version !== 3 || !names.length || Object.keys(report.check_selection ?? {}).length !== names.length
        || names.some(name => report.check_selection?.[name]?.threshold !== selected[name].threshold || !report.check_selection?.[name]?.enabled)) return false;
      if (modelIdentity(report.model_selection) !== modelIdentity(value.stages.qa)) return false;
      if (names.some(name => name === 'script' || name === 'video') && (!report.independent_models || report.total !== policy.question_count || modelIdentity(report.quiz_model_selection) !== modelIdentity(value.stages.quiz))) return false;
      return true;
    }
    return !!report.independent_models;
  };
  const latestReport = evaluation?.video ?? evaluation?.script;
  const stale = !!latestReport && !current(latestReport);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState('');
  const [pending, setPending] = useState<Action>();
  const lock = useRef(false);
  const evaluate = (stage: 'script' | 'video') => runExclusive(lock, setBusy, async () => {
    setMessage('');
    const problem = evaluationModelProblem(value);
    if (problem) throw new Error(problem);
    const { data } = await api.post(`/api/jobs/${jobId}/evaluation/run`, { stage, model_config: value });
    onJob(data.job_id);
  }, error => setMessage(errorMessage(error)));
  const decide = () => runExclusive(lock, setBusy, async () => {
    if (!pending) return;
    setMessage('');
    const report = pending === 'continue' || pending === 'optimize_script' ? evaluation?.script : evaluation?.video;
    const { data } = await api.post(`/api/jobs/${jobId}/evaluation/decision`, { action: pending, fingerprint: report?.fingerprint,
      ...(pending.startsWith('repair_') || pending === 'continue' ? { model_config: value, subtitle_style: subtitleStyle } : {}) });
    setPending(undefined); onJob(data.job_id);
  }, error => setMessage(errorMessage(error)));
  if (!policy.enabled && !evaluation && !error) return null;
  const videoFailed = evaluation?.video && selectedFor('video').some(name => (evaluation.video!.checks?.[name]?.score ?? evaluation.video!.score) < selected[name].threshold);
  return <Section title="质量评估结果" important help="知识问答比较讲稿与成片的知识覆盖；逐页评价独立检查PPT展现、成片呈现和配音内容。每项使用自己的通过线，所选成片项目全部达标才通过。视觉评价来自实际成片抽帧，配音差异来自独立音轨识别，仍需人工复核。">
    {(['script', 'video'] as const).map(stage => {
      const report = evaluation?.[stage];
      const names = selectedFor(stage);
      return <div className="evaluation-stage" key={stage}>
        <div className="field-title"><strong>{stage === 'script' ? '配音前 · 讲稿' : '合片后 · 成片'}</strong>
          <Button variant="outline" disabled={running || busy || !policy.enabled || !names.length || !!evaluationModelProblem(value) || (stage === 'script' ? !hasScript : !hasVideo)} loading={busy} onClick={() => evaluate(stage)}>重新评价{stage === 'script' ? '讲稿' : '成片'}</Button></div>
        {!names.length ? <p className="hint">此阶段未选择评价项目。</p> : <div className="qa-results">{names.map(name => {
          const check = report?.checks?.[name];
          const score = check?.score ?? (name === 'script' || name === 'video' ? report?.score : undefined);
          const passed = current(report) && score != null && score >= selected[name].threshold;
          return <div className="qa-result" key={name} data-passed={score == null ? undefined : passed}>
            <strong>{evaluationTargets[name].label}</strong><div className="qa-score">{score == null ? '待评价' : `${score}%`}</div>
            <span className="qa-threshold-label">通过线 {selected[name].threshold}% · {score == null ? '等待材料' : !current(report) ? '历史结果 · 需重新评价' : passed ? '已通过' : '待复核 / 修复'}</span>
            <span className="hint">{check?.pages ? `已检查 ${check.pages.length} 页 · 显示最低页分 · 平均 ${check.mean_score}%` : report && (name === 'script' || name === 'video') ? `${report.correct} / ${report.total} 题计分 · 答题 ${report.model}` : evaluationTargets[name].detail}</span>
            {name === 'audio' && <span className="hint">差异可能来自识别错误，重新配音前请试听复核。</span>}
            {check?.issues?.length ? <details><summary>查看 {check.issues.length} 个问题与证据</summary>{check.issues.map((issue, index) => <div className="evaluation-issue" key={index}>
              <strong>第 {issue.page} 页 · {issue.timestamp.toFixed(1)} 秒{issue.requires_review ? ' · 待试听复核' : ''}</strong><p>{issue.description}</p><p className="hint">{issue.evidence}</p>
              {issue.script_quote && <p className="hint">讲稿：{issue.script_quote}</p>}{issue.asr_quote && <p className="hint">音轨识别：{issue.asr_quote}</p>}
            </div>)}</details> : null}
            {check?.pages && <details><summary>逐页得分</summary><div className="evaluation-page-scores">{check.pages.map(page => <span key={page.page}>第 {page.page} 页 · {page.score}%</span>)}</div></details>}
          </div>;
        })}</div>}
      </div>;
    })}
    {stale && <p className="notice warn" role="status"><strong>评价项目、门槛、模型或规则已变化，请重新评价。</strong>历史分数保留，旧报告不用于新配置的通过或修复判断。</p>}
    <div className="qa-decision"><strong>{running ? '正在生成或评价，请等待' : nextJobId ? '已创建下一轮任务' : decision === 'manual_accept' ? '已接受当前成片' : stale ? '请按当前配置重新评价' : evaluation?.video ? videoFailed ? '成片存在未通过项目，请复核后定向修复' : '所选成片项目均已通过' : '讲稿通过或手动继续后开始配音合片'}</strong>
      {selected.video.enabled && <span>知识自动修复 {attempt} / {policy.max_auto_retries} 次</span>}</div>
    {error && <p className="notice warn" role="alert">{error}<br />逐页格式或证据错误最多自动重试两次，成功页直接复用；仍失败时保留产物并按通知配置提醒。评价异常不会改写讲稿或伪造得分。</p>}
    {message && <p className="notice err" role="alert">{message}</p>}
    {nextJobId ? <Button theme="primary" onClick={() => onJob(nextJobId)}>查看下一轮任务</Button> : !running && policy.enabled && <div className="actions">
      {!evaluation?.video && <>
        {selected.script.enabled && evaluation?.script && evaluation.script.score < selected.script.threshold && <Button theme="primary" disabled={busy || !current(evaluation.script) || !hasScript} onClick={() => setPending('optimize_script')}>{actionLabels.optimize_script}</Button>}
        <Button variant="outline" disabled={busy || !hasScript} onClick={() => setPending('continue')}>{actionLabels.continue}</Button>
      </>}
      {evaluation?.video && <>
        {selected.video.enabled && <Button variant="outline" disabled={busy || stale} onClick={() => setPending('regenerate')}>{actionLabels.regenerate}</Button>}
        {(['slides', 'audio', 'presentation'] as const).filter(name => evaluation.video?.checks?.[name]).map(name => <Button variant="outline" key={name} disabled={busy || stale || (name === 'audio' && !evaluation.video?.checks?.audio?.pages?.some(page => page.score < selected.audio.threshold || page.issues?.length))} onClick={() => setPending(`repair_${name}`)}>{actionLabels[`repair_${name}`]}</Button>)}
        <Button variant="text" disabled={busy || stale} onClick={() => setPending('accept')}>{actionLabels.accept}</Button>
      </>}
    </div>}
    {(['script', 'video'] as const).map(stage => evaluation?.[stage]?.items?.length ? <details className="qa-details" key={stage}>
      <summary>{stage === 'script' ? '讲稿' : '成片'}答题明细 · {evaluation[stage]!.correct} / {evaluation[stage]!.total} 题计分</summary>
      <div className="qa-question-list">{evaluation[stage]!.items.map(item => <div key={item.id} className="qa-question" data-correct={item.correct}>
        <div className="field-title"><strong>{answerResult(item).label} · {item.id}. {item.question}</strong><Help title="计分原因与材料依据">
          <p>{answerResult(item).explanation}</p><p>正确答案：{item.answer}. {item.options[item.answer]}</p><p>正文依据：{item.source_quote}</p>
          <p>模型回答：{item.predicted ?? '未作答'}</p><p>材料依据：{item.evidence || '无'}</p>
          {item.verified_evidence?.map((quote, index) => <p key={index}>已匹配：{quote}</p>)}
          {item.evidence_status === 'frame_reference' && <p>画面依据是否支持选项仍需人工复核。</p>}</Help></div>
        <span className="hint">{item.kind === 'detail' ? '细节题' : '理解题'} · 模型回答 {item.predicted ?? '未答'} · 正确答案 {item.answer}</span>
      </div>)}</div>
    </details> : null)}
    {pending && <Dialog visible header={actionLabels[pending]} onClose={() => setPending(undefined)} onConfirm={decide}
      confirmBtn={{ content: busy ? '正在提交…' : '确认', loading: busy, disabled: busy }} cancelBtn={{ content: '取消', disabled: busy }}>
      <p>{pending === 'continue' ? '将记录人工继续决定，不改写问答分数；使用已审核讲稿开始配音合片。'
        : pending === 'optimize_script' || pending === 'regenerate' ? '根据有效知识反馈在独立任务重写讲稿，再按所选项目评价；保留原产物。'
        : pending === 'repair_slides' ? '根据逐页视觉问题重新生成课件及配套讲稿，再进入评价与合片。原课件、讲稿和成片保留。'
        : pending === 'repair_audio' ? '请先试听报告中的差异页。确认后只重新生成这些页的配音，复用讲稿与其他页音频，再更新定位和成片。'
        : pending === 'repair_presentation' ? '使用此任务当前的字幕设置、模型与数字人开关重新合片，复用课件和音频。请先在任务设置中调整需要修复的展示选项。'
        : '将任务标记为人工接受，保留各项实际得分。'}</p>
      {pending !== 'accept' && <p><strong>存在成片时，先保存视频及评价快照，再开始修复；备份失败会停止操作。</strong>生成课件、讲稿、配音或数字人可能新增模型费用。</p>}
    </Dialog>}
  </Section>;
}
