import { useEffect, useState } from 'react';
import { api, errorMessage } from './flow';

type CostSummary = { known_estimated_cny: number; unknown_requests: number; notice: string; price_version: string;
  stages: { role: string; requests: number; unknown_requests: number; estimated_usage_requests: number; estimated_cny: number; models: string[]; usage: Record<string, number> }[] };
const labels: Record<string, string> = { text: '课件 / 结构化', vision: '讲稿 / 布局视觉', tts: '配音', asr: '转写', grounding: '光标定位', avatar: '数字人', voice: '音色管理 / 试听', quiz: '原文出题', qa: '材料答题 / 评价' };

export function ModelCosts({ url, title = '模型费用估算' }: { url: string; title?: string }) {
  const [data, setData] = useState<CostSummary>();
  const [error, setError] = useState('');
  useEffect(() => {
    let alive = true;
    setData(undefined); setError('');
    const load = async () => {
      try { const next = await api.get(url); if (alive) { setData(next); setError(''); } }
      catch (failure) { if (alive) setError(errorMessage(failure)); }
    };
    void load();
    const timer = window.setInterval(load, 5000);
    return () => { alive = false; window.clearInterval(timer); };
  }, [url]);
  return <details className="model-costs"><summary>{title}{data ? ` · 已知部分 ¥${data.known_estimated_cny.toFixed(6)}${data.unknown_requests ? ` · ${data.unknown_requests}次待核对` : ''}` : ''}</summary>
    {error && <p className="notice warn" role="status">费用读取失败：{error}</p>}
    {!data && !error && <p className="hint">正在读取请求用量…</p>}
    {data && <><div className="model-cost-rows">{data.stages.map((stage) => <div key={stage.role}>
      <strong>{labels[stage.role] ?? stage.role}</strong><span>{stage.requests ? `${stage.requests}次 · ¥${stage.estimated_cny.toFixed(6)}${stage.unknown_requests ? ` · ${stage.unknown_requests}次未计入` : ''}${stage.estimated_usage_requests ? ` · ${stage.estimated_usage_requests}次用量为估算` : ''}` : '未调用 / 未记录'}</span>
      {stage.models.length > 0 && <small>{stage.models.join(' / ')}
        {stage.usage.prompt_tokens != null && ` · ${stage.usage.prompt_tokens} 输入 tokens / ${stage.usage.completion_tokens ?? 0} 输出 tokens`}
        {stage.usage.characters != null && ` · ${stage.usage.characters} 字符`}
        {stage.usage.images != null && ` · ${stage.usage.images} 张检测图片`}
        {stage.usage.duration_seconds != null && ` · ${stage.usage.duration_seconds} 秒输出视频`}
      </small>}
    </div>)}</div><p className="hint">价格快照：{data.price_version}。{data.notice}</p></>}
  </details>;
}
