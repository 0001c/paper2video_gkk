import { useRef, useState } from 'react';
import { Button, Dialog } from 'tdesign-react';
import { api, errorMessage, runExclusive } from './flow';
import { Help, Section } from './Section';
import { defaultEvaluation, evaluationModelProblem, type ModelConfig } from './modelConfig';
import { answerResult } from './evaluationResult';

export type EvaluationReport = { stage: 'script' | 'video'; score: number; correct: number; total: number; fingerprint: string;
  model: string; quiz_model?: string; independent_models?: boolean; source_policy_version?: number; grading_version?: number; answer_correct?: number; sampled_frames: number; items: { id: string; question: string; answer: string; predicted: string | null; correct: boolean;
    supported: boolean; evidence: string; verified_evidence?: string[]; evidence_status?: string; source_quote: string; options: Record<string, string>; kind: string; timestamp?: number }[] };

export function EvaluationPanel({ jobId, evaluation, error, decision, nextJobId, attempt = 0, value, busy: running, hasVideo, onJob }: {
  jobId: string; evaluation?: { script?: EvaluationReport; video?: EvaluationReport }; error?: string; decision?: string;
  nextJobId?: string; attempt?: number; value: ModelConfig; busy: boolean; hasVideo: boolean; onJob: (id: string) => void;
}) {
  const policy = value.evaluation ?? { ...defaultEvaluation, enabled: false };
  const latestReport = evaluation?.video ?? evaluation?.script;
  const legacyReport = !!latestReport && !latestReport.independent_models;
  const oldRules = !!latestReport && (latestReport.source_policy_version !== 2 || latestReport.grading_version !== 2);
  const needsEvaluation = legacyReport || oldRules;
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState('');
  const [pending, setPending] = useState<'continue' | 'regenerate' | 'accept'>();
  const lock = useRef(false);
  const evaluate = (stage: 'script' | 'video') => runExclusive(lock, setBusy, async () => {
    setMessage('');
    const problem = evaluationModelProblem(value);
    if (problem) throw new Error(problem);
    const { data } = await api.post(`/api/jobs/${jobId}/evaluation/run`, { stage, model_config: value });
    onJob(data.job_id);
  }, (e) => setMessage(errorMessage(e)));
  const decide = () => runExclusive(lock, setBusy, async () => {
    const action = pending;
    if (!action) return;
    setMessage('');
    const report = action === 'continue' ? evaluation?.script : evaluation?.video;
    const { data } = await api.post(`/api/jobs/${jobId}/evaluation/decision`, { action, fingerprint: report?.fingerprint });
    setPending(undefined); onJob(data.job_id);
  }, (e) => setMessage(errorMessage(e)));
  if (!policy.enabled && !evaluation && !error) return null;
  return <Section title="问答质量检查" important help="题库仅来自研究正文，排除摘要、期刊规则、出版信息、作者附注与参考文献条目，两轮使用同一题库。讲稿评价只读取讲稿；成片评价识别实际音轨并读取抽样画面。计分要求选项正确且材料依据通过核验；文本支持多段原句，仅忽略排版空白差异，不用相似度或常识补分。画面依据和理解推断仍需人工核对。阈值和规则调整不改变历史分数。">
    <div className="qa-results">
      {(['script', 'video'] as const).map((stage) => {
        const report = evaluation?.[stage];
        const threshold = stage === 'script' ? policy.script_threshold : policy.standard_threshold;
        const currentRules = report?.independent_models && report.source_policy_version === 2 && report.grading_version === 2;
        return <div className="qa-result" key={stage} data-passed={report ? !!currentRules && report.score >= threshold : undefined}>
          <div className="field-title"><strong>{stage === 'script' ? '讲稿问答' : '成片问答'}</strong>
            <Help title={stage === 'script' ? '讲稿评分依据' : '成片评分依据'}>{stage === 'script' ? '读取讲稿口播内容，答对且提供可验证材料依据才计分。' : `识别成片完整音轨，并结合 ${report?.sampled_frames ?? '最多24'} 张实际画面。此评价不代表全程人工观看或音质、口型审核。`}</Help></div>
          <div className="qa-score">{report ? `${report.score}%` : '待评价'}</div>
          <span className="hint">{report ? `${report.correct} / ${report.total} 题计分 · 答题 ${report.model}` : stage === 'script' ? '讲稿生成后自动评价' : '完整成片产出后自动评价'}</span>
          {report && <span className="hint">选项一致 {report.answer_correct ?? report.items.filter(item => item.predicted === item.answer).length} 题 · 计分还需材料依据</span>}
          {report && <span className="hint">{report.independent_models ? `出题 ${report.quiz_model}` : '历史评分采用同一模型出题与答题，请重新评价'}</span>}
          {report && !currentRules && <span className="hint">历史规则评分 · 不用于当前自动通过判断</span>}
          <span className="qa-threshold-label">{stage === 'script' ? `通过线 ${threshold}%` : `最低 ${policy.minimum_threshold}% · 标准 ${threshold}%`}</span>
          <Button variant="outline" disabled={running || busy || !policy.enabled || !!evaluationModelProblem(value) || (stage === 'video' && !hasVideo)} loading={busy} onClick={() => evaluate(stage)}>重新评价{stage === 'script' ? '讲稿' : '成片'}</Button>
        </div>;
      })}
    </div>
    {oldRules && <p className="notice warn" role="status"><strong>出题范围与依据核验规则已更新，请重新评价。</strong>历史分数保留；重新评价将核验正文题库，含噪声的旧题库需要重新出题，API 调用会计费。</p>}
    <div className="qa-decision"><strong>{running ? '正在生成或评价，请等待' : nextJobId ? '已进入下一轮任务' : decision === 'manual_accept' ? '已接受当前成片' : legacyReport ? '历史评分需要独立模型重新评价' : oldRules ? '历史评分需要按新规则重新评价' : evaluation?.video && evaluation.video.score >= policy.standard_threshold ? '达到标准通过线' : evaluation?.video && evaluation.video.score < policy.minimum_threshold && attempt >= policy.max_auto_retries ? '已达自动重生成上限，请人工决策' : evaluation?.video ? '成片待决策' : '讲稿通过或手动继续后，开始配音合片'}</strong>
      <span>自动重生成 {attempt} / {policy.max_auto_retries} 次</span></div>
    {error && <p className="notice warn" role="alert">{error}<br /><strong>已设置门槛：讲稿 {policy.script_threshold}% · 成片最低 {policy.minimum_threshold}% / 标准 {policy.standard_threshold}%</strong><br />评价请求失败时不会产生本轮得分，也不会触发自动重生成。</p>}
    {message && <p className="notice err" role="alert">{message}</p>}
    {nextJobId ? <Button theme="primary" onClick={() => onJob(nextJobId)}>查看下一轮任务</Button> : !running && policy.enabled && <div className="actions">
      {!evaluation?.video && <Button theme="primary" disabled={busy} onClick={() => setPending('continue')}>手动继续配音合片</Button>}
      {evaluation?.video && <><Button tag="button" theme="primary" disabled={busy || needsEvaluation} onClick={() => setPending('regenerate')}>按评价反馈重新生成</Button>
        <Button tag="button" variant="outline" disabled={busy || needsEvaluation} onClick={() => setPending('accept')}>接受当前成片</Button></>}
    </div>}
    {(['script', 'video'] as const).map((stage) => evaluation?.[stage] && <details className="qa-details" key={stage}>
      <summary>{stage === 'script' ? '讲稿' : '成片'}答题明细 · {evaluation[stage]!.correct} 题计分 / {evaluation[stage]!.total - evaluation[stage]!.correct} 题未计分</summary>
      <div className="qa-question-list">{evaluation[stage]!.items.map((item) => <div key={item.id} className="qa-question" data-correct={item.correct}>
        <div className="field-title"><strong>{answerResult(item).label} · {item.id}. {item.question}</strong><Help title="计分原因与材料依据">
          <p><strong>{answerResult(item).explanation}</strong></p>
          <p>正确答案：{item.answer}. {item.options[item.answer]}</p><p>正文依据：{item.source_quote}</p><p>模型回答：{item.predicted ?? '未作答'}</p>
          <p>模型提供的材料依据：{item.evidence || '无'}{item.timestamp != null ? ` · ${item.timestamp} 秒` : ''}</p>
          {!!item.verified_evidence?.length && <div>已匹配材料原句：{item.verified_evidence.map((quote, index) => <p key={index}>{quote}</p>)}</div>}
          {item.evidence_status === 'frame_reference' && <p>依据引用抽样画面，内容是否支持该选项仍需人工核对。</p>}</Help></div>
        <span className="hint">{item.kind === 'detail' ? '细节题' : '理解题'} · 模型回答 {item.predicted ?? '未答'} · 正确答案 {item.answer}</span>
      </div>)}</div>
    </details>)}
    {pending && <Dialog visible header={pending === 'continue' ? '确认手动继续' : pending === 'regenerate' ? '按错题反馈重新生成' : '接受当前成片'} onClose={() => setPending(undefined)}
      onConfirm={decide} confirmBtn={{ content: busy ? '正在提交…' : '确认', loading: busy, disabled: busy }} cancelBtn={{ content: '取消', disabled: busy }}>
      {pending === 'continue' ? <p>将记录手动继续决定，复用已审核讲稿并开始配音合片。问答分数保留原值，后续成片仍会再次评价。</p>
        : pending === 'regenerate' ? <p>将根据错题与原文依据调整讲稿提示词，在独立任务中重新生成讲稿、配音和视频。历史产物保留，模型调用会新增费用。{value.avatar.enabled && value.avatar.mode === 'api' && <strong>当前启用了数字人 API，重新生成可能再次产生较高费用。</strong>}</p>
        : <p>将任务标记为用户接受，保留原评价分数与结果。</p>}
    </Dialog>}
  </Section>;
}
