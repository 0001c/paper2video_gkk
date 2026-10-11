import { defaultEvaluation, roleStage, type ModelConfig, type EvaluationPolicy } from './modelConfig';
import { Help } from './Section';

export function EvaluationSettings({ value, onChange }: { value: ModelConfig; onChange: (value: ModelConfig) => void }) {
  const policy = value.evaluation ?? { ...defaultEvaluation, enabled: false };
  const change = (next: Partial<EvaluationPolicy>) => onChange({ ...value, evaluation: { ...policy, ...next },
    stages: { ...value.stages, qa: roleStage(value, 'qa') } });
  return <div className="qa-settings">
    <div className="field-title"><label className="checkline"><input type="checkbox" checked={policy.enabled} onChange={(event) => change({ enabled: event.target.checked })} />启用讲稿与成片问答评价</label>
      <Help title="问答评价流程" important>从原文生成固定题库，讲稿与成片分别答题。讲稿达到通过线后自动配音合片，否则等你复核并手动继续。成片低于最低线时依据错题自动重写；达到最低线但未达标准线时等待你的决策。达到重试上限后停止自动重生成。成片材料来自实际音轨的本地 Qwen3-ASR 识别与抽样画面，需要选择支持图片的评价模型；问答分数不能替代事实、音质与口型审核。评价 API 调用也会计费。</Help>
    </div>
    {policy.enabled && <>
      <div className="qa-thresholds">
        {([['script_threshold', '讲稿通过线', 0, 100, '%'], ['minimum_threshold', '成片最低线', 0, 100, '%'],
          ['standard_threshold', '成片标准线', 0, 100, '%'], ['question_count', '题目数量', 4, 40, '题'], ['max_auto_retries', '自动重生成上限', 0, 3, '次']] as const).map(([key, label, min, max, unit]) =>
          <div className="field" key={key}><label>{label}<span className="hint">{unit}</span><input type="number" min={min} max={max} step={1} value={policy[key]}
            onChange={(event) => change({ [key]: event.target.valueAsNumber })} /></label></div>)}
      </div>
      <p className="qa-flow"><strong>讲稿评价</strong><span>→ 配音合片 →</span><strong>成片评价</strong><span>→ 完成 / 复核 / 重生成</span></p>
      {policy.minimum_threshold > policy.standard_threshold && <p className="notice err" role="alert">最低线不能高于标准线。</p>}
    </>}
  </div>;
}
