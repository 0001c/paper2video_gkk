import { useId } from 'react';
import { defaultEvaluation, evaluationChecks, evaluationTargets, evaluationModelProblem, roleStage, type ModelConfig, type EvaluationPolicy, type EvaluationTarget, type EvaluationCheck } from './modelConfig';
import { Help, Section } from './Section';

export function EvaluationSettings({ value, onChange, task = false, disabled = false }: {
  value: ModelConfig; onChange: (value: ModelConfig) => void; task?: boolean; disabled?: boolean;
}) {
  const prefix = useId();
  const policy = value.evaluation ?? { ...defaultEvaluation, enabled: false };
  const checks = evaluationChecks(policy);
  const selected = Object.values(checks).filter(check => check.enabled).length;
  const questions = checks.script.enabled || checks.video.enabled;
  const change = (next: Partial<EvaluationPolicy>) => onChange({ ...value, evaluation: { ...policy, ...next },
    stages: { ...value.stages, quiz: roleStage(value, 'quiz'), qa: roleStage(value, 'qa') } });
  const targetChange = (name: EvaluationTarget, next: Partial<EvaluationCheck>) => {
    const updated = { ...checks, [name]: { ...checks[name], ...next } };
    change({ checks: updated, ...(name === 'script' ? { script_threshold: updated.script.threshold } : {}),
      ...(name === 'video' ? { standard_threshold: updated.video.threshold, minimum_threshold: Math.min(policy.minimum_threshold, updated.video.threshold) } : {}) });
  };
  return <Section anchor={task ? undefined : 'evaluation'} number={task ? undefined : '08'} title={task ? '此任务的评估配置' : '评估配置'} compact={task}
    summary={policy.enabled ? `已选 ${selected} 项 · 分别设置通过线` : '已关闭'} defaultOpen={false} important
    help="选择需要评价的部分，各项独立判断；所有勾选项目通过才算成片通过。评价异常时暂停复核，未选项目不调用评价模型。每次修复前保存成片、讲稿、配置与评价快照，新成片另存版本。">
    <fieldset className="evaluation-settings" disabled={disabled}>
      <legend className="sr-only">评估项目与通过门槛</legend>
      <label className="checkline"><input type="checkbox" checked={policy.enabled} onChange={(event) => change({ enabled: event.target.checked })} />启用质量评估</label>
      {task && <p className="hint">修改后用于下一次重新评价或修复；历史评分和成片保留。</p>}
      {policy.enabled && <>
        <p className="hint">勾选需要检查的部分。讲稿在配音前评价，其余项目在完整成片产出后评价。</p>
        <div className="evaluation-targets">{(Object.keys(evaluationTargets) as EvaluationTarget[]).map(name => <div className="evaluation-target" key={name} data-enabled={checks[name].enabled}>
          <label className="evaluation-target-choice"><input type="checkbox" checked={checks[name].enabled} onChange={event => targetChange(name, { enabled: event.target.checked })} />
            <span><strong>{evaluationTargets[name].label}</strong><small>{evaluationTargets[name].detail}</small></span></label>
          <div className="evaluation-target-threshold"><label htmlFor={`${prefix}-${name}`}>通过线</label><div><input id={`${prefix}-${name}`} aria-label={`${evaluationTargets[name].label}通过线`} type="number" min={0} max={100} step={1}
            disabled={!checks[name].enabled} value={checks[name].threshold} onChange={event => targetChange(name, { threshold: event.target.valueAsNumber })} /><span>%</span></div></div>
        </div>)}</div>
        <p className="hint">PPT、呈现和配音按最低页分判断，并提供页码与时间点。配音差异需要试听复核；此项检查内容，不评价音色相似度。</p>
        {evaluationModelProblem(value) && <p className="notice err" role="alert">{evaluationModelProblem(value)}</p>}
        <details className="evaluation-advanced"><summary>模型分工与自动修复</summary>
          <p className="hint">评价模型在“模型配置”中选择。{questions ? '知识问答需另外选择独立的出题型号；其他项目直接使用评价模型。' : '当前项目无需出题模型。'}视觉与配音问题暂停等待定向修复；自动重写仅适用于成片知识低分。</p>
          <div className="qa-thresholds">
            {questions && <label htmlFor={`${prefix}-questions`}>题目数量<input id={`${prefix}-questions`} type="number" min={4} max={40} step={1} value={policy.question_count} onChange={event => change({ question_count: event.target.valueAsNumber })} /></label>}
            {checks.video.enabled && <>
              <label htmlFor={`${prefix}-minimum`}>知识自动修复线（%）<input id={`${prefix}-minimum`} type="number" min={0} max={checks.video.threshold} step={1} value={policy.minimum_threshold} onChange={event => change({ minimum_threshold: event.target.valueAsNumber })} /></label>
              <label htmlFor={`${prefix}-retries`}>自动修复上限（次）<input id={`${prefix}-retries`} type="number" min={0} max={3} step={1} value={policy.max_auto_retries} onChange={event => change({ max_auto_retries: event.target.valueAsNumber })} /></label>
            </>}
          </div>
          <Help title="评估与修复费用">所选项目会调用评价模型；逐页视觉评价会增加请求数。本地音轨识别复用缓存。课件优化、知识重写和重新配音可能新增生成费用；合片与备份在本地执行，启用数字人时仍可能调用其模型。</Help>
        </details>
      </>}
    </fieldset>
  </Section>;
}
