import { useEffect, useRef, useState } from "react";
import { Button, Select } from "tdesign-react";
import { api, errorMessage } from "./flow";
import { Help } from './Section';

export function ModelSettings({ apiKey, apiBase, textModel, visionModel, concurrency, onText, onVision, onConcurrency }: {
  apiKey: string; apiBase: string; textModel: string; visionModel: string; concurrency: number;
  onText: (v: string) => void; onVision: (v: string) => void; onConcurrency: (v: number) => void;
}) {
  const [models, setModels] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const version = useRef(0);
  const fetchModels = async () => {
    const current = ++version.current;
    setBusy(true); setMessage("");
    try {
      const { data } = await api.post("/api/models", { api_key: apiKey.trim(), api_base: apiBase.trim() });
      if (current === version.current) { setModels(data.models ?? []); setMessage(data.message); }
    } catch (error) {
      if (current === version.current) { setModels([]); setMessage(`${errorMessage(error)}；可以手动输入模型 ID。`); }
    } finally { if (current === version.current) setBusy(false); }
  };
  useEffect(() => {
    version.current++; setModels([]); setMessage(""); setBusy(false);
    if (!apiKey.trim()) return;
    const timer = window.setTimeout(() => { void fetchModels(); }, 800);
    return () => { window.clearTimeout(timer); version.current++; };
  }, [apiKey, apiBase]);
  return <>
    <div className="actions"><Button variant="outline" loading={busy} disabled={!apiKey.trim() || busy} onClick={fetchModels}>刷新模型列表</Button></div>
    <div className="grid grid-2">
      {[["文字模型", "text-model", textModel, onText], ["讲稿 / 视觉模型", "vision-model", visionModel, onVision]].map(([label, id, value, onChange]) => {
        const change = onChange as (v: string) => void;
        const selected = value as string;
        const options = [...new Set([...models, selected].filter(Boolean))].map((v) => ({ label: v, value: v }));
        return <div className="field" key={id as string} ref={(el) => { el?.querySelector('input')?.setAttribute('id', id as string); }}><div className="field-title"><label htmlFor={id as string}>{label as string}</label><Help title={label as string} important={id === 'vision-model'}>
          {id === 'vision-model' ? '讲稿模型必须支持图像输入，用于读取课件生成讲解。审核重写沿用任务保存的模型。' : '文字模型用于正文结构化与生成课件。'}填写凭据后自动获取服务商模型列表；可搜索选择，也可手动输入模型 ID。服务商列表通常不标注视觉能力，请自行核对。
        </Help></div>
          <Select
            value={selected} options={options} filterable creatable placeholder="搜索或输入模型 ID"
            popupProps={{ overlayStyle: { maxWidth: "calc(100vw - 32px)" }, overlayInnerStyle: { maxWidth: "calc(100vw - 32px)" } }}
            onChange={(v) => change(String(v))} onCreate={(v) => change(String(v).trim())} />
        </div>;
      })}
    </div>
    {message && <p className="hint" role="status">{message}</p>}
    {models.length > 0 && [textModel, visionModel].some((v) => !models.includes(v)) && <p className="notice warn">当前选择未全部出现在服务商模型列表中，请核对模型 ID；手动输入的模型仍可使用。</p>}
    <div className="field"><div className="field-title"><label htmlFor="script-concurrency">讲稿并发请求数</label><Help title="讲稿并发">默认 3，范围 1～8；服务商限流时可设为 1。仅并发请求讲稿，本地 GPU 仍串行运行。</Help></div><input id="script-concurrency" type="number" min={1} max={8} step={1} value={concurrency} onChange={(e) => onConcurrency(Number(e.target.value))} /></div>
  </>;
}
