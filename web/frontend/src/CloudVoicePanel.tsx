import { FormSelect } from './FormSelect';
import { useEffect, useId, useRef, useState } from "react";
import { Button } from "tdesign-react";
import { api, errorMessage, runExclusive } from "./flow";
import type { Stage } from "./modelConfig";
import { ModelCosts } from './ModelCosts';

export type CloudVoice = { resource_id: string; voice_id: string; label: string; target_model: string; sample_url?: string;
  scope_id: string; kind: string; status: string; timestamp_status: string };

export function CloudVoicePanel({ stage, value, onChange }: {
  stage: Stage; value?: CloudVoice; onChange: (voice?: CloudVoice) => void;
}) {
  const prefix = useId();
  const [voices, setVoices] = useState<CloudVoice[]>([]);
  const [label, setLabel] = useState("我的讲解音色");
  const [voiceId, setVoiceId] = useState("");
  const [prompt, setPrompt] = useState("");
  const [reference, setReference] = useState("");
  const [message, setMessage] = useState("");
  const [sample, setSample] = useState("");
  const [busy, setBusy] = useState(false);
  const lock = useRef(false);
  const generation = useRef(0);
  const tokens = useRef(new Map<string, string>());
  const context = `${stage.connection_id}|${stage.model}`;
  const currentContext = useRef(context);
  currentContext.current = context;
  const connection = { connection_id: stage.connection_id, target_model: stage.model };
  const reload = async (selection = value) => {
    if (!stage.connection_id) { setVoices([]); return; }
    const captured = context;
    const data = await api.get(`/api/cloud-voices?connection_id=${encodeURIComponent(stage.connection_id)}&target_model=${encodeURIComponent(stage.model)}`);
    if (currentContext.current !== captured) return;
    setVoices(data.items);
    const saved = data.items.find((voice: CloudVoice) => voice.resource_id === selection?.resource_id);
    if (selection && !saved) onChange(undefined);
    else if (saved) onChange(saved);
  };
  useEffect(() => {
    const version = ++generation.current;
    setSample(""); setMessage(""); setVoices([]);
    void reload().catch((error) => { if (version === generation.current) setMessage(errorMessage(error)); });
  }, [context]);
  const action = (operation: () => Promise<unknown>) => runExclusive(lock, setBusy, async () => {
    setMessage(""); await operation();
  }, (error) => setMessage(`${errorMessage(error)}。若云动作中断，请核对云端记录；再次点击沿用防重键，不自动重新计费。`));
  const headers = (body: unknown) => {
    const identity = JSON.stringify([context, body]);
    if (!tokens.current.has(identity)) tokens.current.set(identity, crypto.randomUUID());
    return { "Idempotency-Key": tokens.current.get(identity)! };
  };
  const generate = (kind: "clone" | "design") => action(async () => {
    const captured = context;
    const body = { ...connection, kind, label, consent: true, ...(kind === "clone" ? { path: reference } : { prompt }) };
    const result = await api.post("/api/cloud-voices/generate", body, headers(body));
    if (captured !== currentContext.current) return;
    onChange(result.data); await reload(result.data); setSample(result.data.sample_url ?? ""); setMessage("资源已保存。先查询状态，再显式试音验证时间戳；设计音色暂仅供试听。");
  });
  const selected = voices.find((voice) => voice.resource_id === value?.resource_id);
  return <div className="cloud-voice-panel" aria-busy={busy}>
    <p className="notice info">API配音不读取本地默认录音，也不调用旧MiMo。系统音色和已验证复刻音色支持时间戳；设计音色兼容性未确认，不能用于视频链路。</p>
    <div className="field"><label htmlFor={`${prefix}-select`}>百炼音色 · {stage.model}</label>
      <FormSelect id={`${prefix}-select`} disabled={busy} value={selected?.resource_id ?? ""} onChange={choice => {
        const chosen = voices.find((voice) => voice.resource_id === choice); onChange(chosen); setSample(chosen?.sample_url ?? "");
      }}><option value="">请选择此型号、此连接的音色</option>{voices.map((voice) => <option key={voice.resource_id} value={voice.resource_id}>
        {voice.label} · {voice.kind === "system" ? "系统" : voice.kind === "design" ? "设计 / 仅试听" : "复刻"} · {voice.status === "OK" && voice.timestamp_status === "supported" ? "可用于视频" : "待验证"}
      </option>)}</FormSelect>
      <p className="hint">没有现成音色？可选择 cosyvoice-v3-flash 使用龙安洋，或为当前型号注册/创建专属音色。音色不能跨型号或业务空间复用。</p>
    </div>
    <div className="actions">
      <Button variant="outline" disabled={busy || !stage.connection_id} onClick={() => action(() => reload())}>刷新本地目录（不推理）</Button>
      <Button variant="outline" disabled={busy || !stage.connection_id} onClick={() => action(async () => {
        const { data } = await api.post('/api/cloud-voices/recover', { connection_id: stage.connection_id, target_model: stage.model });
        if (context !== currentContext.current) return;
        await reload();
        setMessage(data.items.length ? `已找回 ${data.items.length} 个云端音色，请在上方选择。此操作没有创建或合成音色。` : '未找到此型号由本应用创建的音色，请核对连接和云端记录。');
      })}>查询并找回云端音色（不新建）</Button>
      <Button variant="outline" disabled={busy || !selected || selected.kind === "system"} onClick={() => action(async () => {
        const result = await api.post(`/api/cloud-voices/${selected!.resource_id}/refresh`, connection);
        if (context !== currentContext.current) return;
        onChange(result.data); await reload(result.data);
      })}>查询云端状态（不合成）</Button>
      <Button variant="outline" disabled={busy || selected?.status !== "OK"} onClick={() => action(async () => {
        const body = { ...connection, text: "欢迎收听今天的讲解。" };
        const result = await api.post(`/api/cloud-voices/${selected!.resource_id}/preview`, body, headers({ resource: selected!.resource_id, ...body }));
        if (context !== currentContext.current) return;
        setSample(result.data.sample_url); await reload();
      })}>合成试听（可能计费）</Button>
    </div>
    {sample && <audio controls src={sample} preload="none" aria-label="已归档云音色试听" />}
    <details><summary>注册或创建云音色</summary>
      <div className="field"><label htmlFor={`${prefix}-label`}>音色名称</label><input id={`${prefix}-label`} maxLength={80} value={label} disabled={busy} onChange={(event) => setLabel(event.target.value)} /></div>
      <div className="field"><label htmlFor={`${prefix}-id`}>已有 voice_id</label><input id={`${prefix}-id`} maxLength={200} value={voiceId} disabled={busy} onChange={(event) => setVoiceId(event.target.value)} /></div>
      <Button variant="outline" disabled={busy || !voiceId.trim() || !stage.connection_id} onClick={() => action(async () => {
        const result = await api.post("/api/cloud-voices/register", { ...connection, voice_id: voiceId.trim(), label });
        if (context !== currentContext.current) return;
        onChange(result.data); await reload(result.data);
      })}>登记已有ID（不推理）</Button>
      <div className="field"><label htmlFor={`${prefix}-upload`}>复刻参考录音 · 仅先上传本机</label><input id={`${prefix}-upload`} type="file" accept="audio/*" disabled={busy} onChange={(event) => {
        const file = event.target.files?.[0]; if (!file) return;
        void action(async () => { const form = new FormData(); form.append("paper", file);
          const result = await api.postForm("/api/voices/upload", form); setReference(result.data.spec.path); setMessage("录音已留在本机，尚未上传云端。"); });
      }} /></div>
      <p className="hint">点击复刻表示同意将该录音上传服务端私有OSS并交百炼处理。需16位WAV、至少16kHz、5～60秒、≤10MiB；推荐10～20秒。仅使用本人或已获授权的声音。</p>
      <Button disabled={busy || !reference || !stage.connection_id} onClick={() => generate("clone")}>同意上传并复刻（可能计费）</Button>
      <div className="field"><label htmlFor={`${prefix}-prompt`}>声音设计描述</label><textarea id={`${prefix}-prompt`} maxLength={500} rows={3} value={prompt} disabled={busy} onChange={(event) => setPrompt(event.target.value)} /></div>
      <Button variant="outline" disabled={busy || !prompt.trim() || !stage.connection_id || stage.model === "cosyvoice-v2"} onClick={() => generate("design")}>创建设计音色（可能计费，仅试听）</Button>
    </details>
    {message && <p className="notice warn" role="status">{message}</p>}
    <ModelCosts url="/api/model-costs/voices" title="音色管理累计费用估算（非本任务独占）" />
  </div>;
}
