import { useEffect, useRef, useState } from "react";
import { Button } from "tdesign-react";
import { api, errorMessage } from "./flow";

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
    <div className="actions"><Button variant="outline" loading={busy} disabled={!apiKey.trim() || busy} onClick={fetchModels}>刷新模型列表</Button><span className="hint">填写凭据后自动读取；可搜索下拉候选，也可直接输入模型 ID。</span></div>
    <div className="grid grid-2">
      {[["文字模型", "text-model", textModel, onText], ["讲稿 / 视觉模型", "vision-model", visionModel, onVision]].map(([label, id, value, onChange]) => {
        const change = onChange as (v: string) => void;
        const selected = value as string;
        return <div className="field" key={id as string}><label htmlFor={id as string}>{label as string}</label>
          <input id={id as string} type="text" list="provider-models" value={selected} maxLength={200}
            placeholder="搜索或输入模型 ID" autoComplete="off" onChange={(e) => change(e.target.value)} />
        </div>;
      })}
    </div>
    <datalist id="provider-models">{models.map((id) => <option key={id} value={id} />)}</datalist>
    {message && <p className="hint" role="status">{message}</p>}
    {models.length > 0 && [textModel, visionModel].some((v) => !models.includes(v)) && <p className="notice warn">当前选择未全部出现在服务商模型列表中，请核对模型 ID；手动输入的模型仍可使用。</p>}
    <p className="hint">文字模型用于正文结构化和课件生成；讲稿模型必须支持图像输入。这里的选择用于新任务，审核重写使用所选任务保存的模型。</p>
    <div className="field"><label htmlFor="script-concurrency">讲稿并发请求数</label><input id="script-concurrency" type="number" min={1} max={8} step={1} value={concurrency} onChange={(e) => onConcurrency(Number(e.target.value))} /><span className="hint">默认 3，范围 1～8；服务商限流时可设为 1。仅并发生成讲稿，不并发占用本地 GPU。</span></div>
  </>;
}
