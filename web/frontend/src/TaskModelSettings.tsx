import { useEffect, useRef, useState } from "react";
import { Button } from "tdesign-react";
import { api, errorMessage, runExclusive } from "./flow";
import { ModelSettings } from "./ModelSettings";
import { CloudVoicePanel, type CloudVoice } from './CloudVoicePanel';
import { roleLabels, roles, roleStage, type Connection, type ModelConfig, type ModelOptions } from "./modelConfig";

export function TaskModelSettings({ jobId, portraitPath, snapshot, value, connections: initialConnections, options, concurrency, disabled, onChange, voice, voiceResource, onVoice }: {
  voice?: unknown; voiceResource?: CloudVoice; onVoice: (resource?: CloudVoice) => void;
  portraitPath?: string;
  jobId: string; snapshot: ModelConfig; value: ModelConfig; connections?: Record<string, { label?: string; scope_id: string; provider: string; compatible_base_url: string; catalog_time?: string; catalog_source?: string }>;
  options?: ModelOptions; concurrency: number; disabled: boolean; onChange: (value: ModelConfig) => void;
}) {
  const [saved, setSaved] = useState(snapshot);
  const [snapshots, setSnapshots] = useState(initialConnections ?? {});
  const [connections, setConnections] = useState<Connection[]>([]);
  const [bindings, setBindings] = useState<Record<string, string>>({});
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);
  const lock = useRef(false);
  const reload = () => runExclusive(lock, setBusy, async () => {
    setMessage(""); const data = await api.get("/api/model-connections"); setConnections(data.items);
  }, (error) => setMessage(errorMessage(error)));
  useEffect(() => { void reload(); }, [jobId]);
  const rebind = () => runExclusive(lock, setBusy, async () => {
    setMessage("");
    const selected = Object.fromEntries(Object.entries(bindings).filter(([, identity]) => identity));
    if (!Object.keys(selected).length) throw new Error("请先选择需要重新绑定的连接");
    await api.post(`/api/jobs/${jobId}/model-connections/rebind`, { bindings: selected });
    const data = await api.get(`/api/jobs/${jobId}`);
    setSaved(data.config.model_config); setSnapshots(data.config.model_connections); onChange(data.config.model_config);
    setBindings({}); setMessage("同范围连接已重新绑定，未启动推理。请再明确选择继续准备/审核生成/重跑。");
  }, (error) => setMessage(errorMessage(error)));
  return <details className="task-model-settings"><summary>执行模型快照与连接恢复</summary>
    <p className="hint">只操作此任务，不继承新建任务草稿。修改模型选择只用于下一次明确继续/重跑，历史产物不会立即更新。</p>
    <dl className="model-snapshot">{roles.map((role) => {
      const stage = roleStage(saved, role); const connection = snapshots[stage.connection_id ?? ""];
      return <div key={role}><dt>{roleLabels[role]}</dt><dd>{stage.model} · {stage.mode === "api" ? "API" : "本地"}
        {role === "avatar" && !saved.avatar.enabled ? "（关闭）" : ""}<span className="hint">{connection?.label ?? stage.connection_id ?? "无需云连接"} · {connection?.scope_id ?? "本地"}</span>
        {connection && <span className="hint">目录：{connection.catalog_time ?? "未记录"} · {connection.catalog_source ?? "未验证"}</span>}</dd></div>;
    })}</dl>
    <p className="hint">时间轴：{saved.timing.source}。{saved.avatar.enabled ? "单段严格小于20秒，自动分段并保留完整页音轨" : "未启用数字人，无20秒分段限制"}。目录可见与配置合法均不是运行验收。</p>
    {!disabled && <>
      <details><summary>重新绑定失效连接（同范围、同端点）</summary><p className="hint">先在管理连接中重建，填写原资源范围标识，再刷新可用连接。不会借用其他账号密钥或自动继续执行。</p>
        <Button variant="text" disabled={busy} onClick={reload}>刷新可用连接</Button>
        {Object.entries(snapshots).map(([identity, entry]) => <div className="field" key={identity}><label htmlFor={`rebind-${identity}`}>{entry.label ?? identity} · {entry.scope_id}</label>
          <select id={`rebind-${identity}`} value={bindings[identity] ?? ""} onChange={(event) => setBindings((previous) => ({ ...previous, [identity]: event.target.value }))}>
            <option value="">不变</option>{connections.filter((connection) => connection.provider === entry.provider && connection.scope_id === entry.scope_id && connection.compatible_base_url === entry.compatible_base_url)
              .map((connection) => <option value={connection.id} key={connection.id}>{connection.label}</option>)}
          </select></div>)}
        <Button variant="outline" loading={busy} disabled={busy} onClick={rebind}>重新绑定（不执行）</Button>
      </details>
      <details><summary>修改此任务下一次执行的模型组合</summary>
        <label className="checkline"><input type="checkbox" checked={value.avatar.enabled} onChange={(event) => onChange({ ...value, avatar: { ...value.avatar, enabled: event.target.checked } })} />启用数字人（单段严格小于20秒）</label>
        <ModelSettings value={value} options={options} concurrency={concurrency} concurrencyEditable={false} onChange={onChange} onConcurrency={() => {}} voice={voice} portraitPath={portraitPath} />
        {value.stages.tts.mode === 'api' ? <CloudVoicePanel stage={value.stages.tts} value={voiceResource} onChange={onVoice} />
          : <Button variant="outline" onClick={() => onVoice(undefined)}>此任务改用本地默认参考音色</Button>}
      </details>
    </>}
    {message && <p className="notice info" role="status">{message}</p>}
  </details>;
}
