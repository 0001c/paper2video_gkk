import { useId, useRef, useState } from "react";
import { Button, Drawer } from "tdesign-react";
import { api, errorMessage, runExclusive } from "./flow";
import { bailianEndpoints, type Connection } from "./modelConfig";
import { Help } from './Section';

const defaultBase = "https://llm-3p2iijyqd4hu17zi.cn-beijing.maas.aliyuncs.com/compatible-mode/v1";
export function ModelConnections({ visible, connections, localKeyStorageAvailable = false, storageError, onClose, onReload }: {
  visible: boolean; connections: Connection[]; onClose: () => void; onReload: () => Promise<void>;
  localKeyStorageAvailable?: boolean; storageError?: string;
}) {
  const prefix = useId();
  const [editing, setEditing] = useState("");
  const [label, setLabel] = useState("北京百炼工作空间");
  const [provider, setProvider] = useState<Connection["provider"]>("bailian");
  const [base, setBase] = useState(defaultBase);
  const [nativeBase, setNativeBase] = useState("");
  const [ws, setWs] = useState("");
  const [scope, setScope] = useState("");
  const [key, setKey] = useState("");
  const [persistKey, setPersistKey] = useState(true);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const lock = useRef(false);
  const fillSpeechEndpoints = () => {
    try {
      const endpoints = bailianEndpoints(base);
      setNativeBase(endpoints.native); setWs(endpoints.websocket); setMessage("");
    } catch (error) { setMessage(errorMessage(error)); }
  };
  const edit = (entry?: Connection) => {
    setEditing(entry?.id ?? ""); setLabel(entry?.label ?? "北京百炼工作空间"); setProvider(entry?.provider ?? "bailian");
    setBase(entry?.compatible_base_url ?? defaultBase); setNativeBase(entry?.native_http_base_url ?? "");
    setWs(entry?.websocket_url ?? ""); setScope(entry?.scope_id ?? ""); setKey(""); setMessage("");
    setPersistKey(entry ? !!entry.persist_key : true);
  };
  const save = () => runExclusive(lock, setBusy, async () => {
    setMessage("");
    if (!label.trim() || !base.trim()) throw new Error("请填写连接名称和兼容端点");
    if (!editing && provider === "bailian" && !key.trim()) throw new Error("百炼连接需要 API Key");
    if (editing) await api.patch(`/api/model-connections/${editing}`, { label: label.trim(), persist_key: localKeyStorageAvailable && persistKey, ...(key ? { api_key: key.trim() } : {}) });
    else {
      const { data } = await api.post("/api/model-connections", { label: label.trim(), provider, compatible_base_url: base.trim(), persist_key: localKeyStorageAvailable && persistKey,
      ...(provider === "bailian" && nativeBase.trim() ? { native_http_base_url: nativeBase.trim() } : {}),
      ...(provider === "bailian" && ws.trim() ? { websocket_url: ws.trim() } : {}),
        ...(scope.trim() ? { scope_id: scope.trim() } : {}), ...(key.trim() ? { api_key: key.trim() } : {}) });
      setEditing(data.id); setScope(data.scope_id);
    }
    setKey(""); await onReload(); setMessage(localKeyStorageAvailable && persistKey ? "连接已加密保存到本机，重启后自动恢复；未调用推理接口。" : "连接仅在本次服务内存中保存；未调用推理接口。");
  }, (error) => setMessage(errorMessage(error)));
  const remove = (entry: Connection) => runExclusive(lock, setBusy, async () => {
    if (!window.confirm(`删除连接「${entry.label}」？草稿和旧任务中的引用不会自动替换。`)) return;
    await api.delete(`/api/model-connections/${entry.id}`); await onReload();
    if (editing === entry.id) edit();
    setMessage("连接已删除；任务快照与历史产物保留。");
  }, (error) => setMessage(errorMessage(error)));
  return <Drawer visible={visible} header="模型连接" size="min(480px, 100vw)" footer={false}
    closeOnOverlayClick={!busy} onClose={() => { if (!busy) { setKey(""); onClose(); } }}>
    <div className="field-title"><span className="hint">选择本机保存后，服务重启可自动恢复连接。</span><Help title="本机保存说明">密钥使用当前 Windows 账户加密，保存至服务端本机，不保存在浏览器草稿中，也不会回显。通常需在同一电脑、同一 Windows 账户下恢复。取消保存会移除本机加密副本，当前内存连接仍可使用。此工作台面向本机或受信内网，连接属于服务端当前账户。</Help></div>
    {storageError && <p className="notice err" role="alert">{storageError}</p>}
    <p className="hint">轮换密钥不改写已冻结任务的凭据；旧任务需要明确重新绑定，可选择同一连接重新冻结。</p>
    <div className="connection-list">
      {connections.length === 0 && <p>尚未配置连接。请先添加百炼或本地兼容服务连接。</p>}
      {connections.map((entry) => <div className="connection-item" key={entry.id}>
        <strong>{entry.label}</strong><span className="hint">{entry.provider} · {entry.key_present ? "密钥已配置" : "无密钥"} · {entry.source === "env" ? "环境变量" : entry.persist_key ? "本机加密保存" : "临时连接"}</span>
        <code>{entry.compatible_base_url}</code><span className="hint">范围：{entry.scope_id}</span>
        {entry.source === "memory" && <div className="actions"><Button variant="text" disabled={busy} onClick={() => edit(entry)}>编辑 / 轮换密钥</Button>
          <Button theme="danger" variant="text" disabled={busy} onClick={() => remove(entry)}>删除</Button></div>}
      </div>)}
    </div>
    <div className="actions"><Button variant="outline" disabled={busy} onClick={() => edit()}>添加新连接</Button></div>
    <form className="model-connection-form" onSubmit={(event) => { event.preventDefault(); void save(); }}>
      <fieldset disabled={busy}>
        <div className="field"><label htmlFor={`${prefix}-label`}>连接名称</label><input id={`${prefix}-label`} value={label} maxLength={80} onChange={(event) => setLabel(event.target.value)} /></div>
        <div className="field"><label htmlFor={`${prefix}-provider`}>服务类型</label><select id={`${prefix}-provider`} disabled={!!editing} value={provider} onChange={(event) => { setProvider(event.target.value as Connection["provider"]); setKey(""); setBase(event.target.value === "bailian" ? defaultBase : "http://127.0.0.1:8000/v1"); }}>
          <option value="bailian">阿里云百炼</option><option value="openai_compatible">OpenAI 兼容 / 本地服务</option></select></div>
        <div className="field"><div className="field-title"><label htmlFor={`${prefix}-base`}>兼容 Base URL</label>{provider === 'openai_compatible' && <Help title="兼容服务地址">DeepSeek 官方请选择“OpenAI 兼容 / 本地服务”，填写 <code>https://api.deepseek.com</code> 或 <code>https://api.deepseek.com/v1</code>，使用官方 API Key。本地服务填写本机或已允许的私网地址。其他云端域名需由服务端配置白名单 PAPER2VIDEO_MODEL_HOSTS，多个主机以英文逗号分隔，仅填写主机名。</Help>}</div><input id={`${prefix}-base`} readOnly={!!editing} value={base} onChange={(event) => setBase(event.target.value)} /></div>
        {provider === "bailian" && <>
          <div className="actions"><Button variant="outline" disabled={!!editing} onClick={fillSpeechEndpoints}>按兼容地址填入配音端点</Button></div>
          <p className="hint">配音端点使用当前工作空间的同一域名；HTTP 以 /api/v1 结尾，WebSocket 以 /api-ws/v1/inference 结尾。</p>
          <div className="field"><label htmlFor={`${prefix}-native`}>原生 HTTP Base URL（云配音 / 音色必填）</label><input id={`${prefix}-native`} readOnly={!!editing} value={nativeBase} placeholder="https://…/api/v1" onChange={(event) => setNativeBase(event.target.value)} /></div>
          <div className="field"><label htmlFor={`${prefix}-ws`}>WebSocket URL（可选）</label><input id={`${prefix}-ws`} readOnly={!!editing} value={ws} placeholder="wss://…" onChange={(event) => setWs(event.target.value)} /></div>
        </>}
        <div className="field"><label htmlFor={`${prefix}-scope`}>资源范围标识（可选）</label><input id={`${prefix}-scope`} readOnly={!!editing} value={scope} placeholder="新连接默认生成；同账号恢复时填原范围" onChange={(event) => setScope(event.target.value)} /></div>
        <p className="hint">范围标识是受信管理员的声明，不是平台账号验证。端点/范围需要改变时请添加新连接、填入完整地址，再在模型配置中重新选择。</p>
        <div className="field"><label htmlFor={`${prefix}-key`}>API Key{editing ? "（留空保留原密钥）" : ""}</label><input id={`${prefix}-key`} type="password" autoComplete="off" value={key} maxLength={2000} onChange={(event) => setKey(event.target.value)} /></div>
        <label className="checkline"><input type="checkbox" checked={localKeyStorageAvailable && persistKey} disabled={!localKeyStorageAvailable || !!storageError} onChange={(event) => setPersistKey(event.target.checked)} />将密钥加密保存到本机（重启后恢复）</label>
        {!localKeyStorageAvailable && <p className="hint">本地保存需加载支持 Windows 账户加密的后端。</p>}
        <Button type="submit" loading={busy} theme="primary">{editing ? "保存 / 轮换" : "保存连接"}</Button>
      </fieldset>
    </form>
    {message && <p className="notice info" role="status">{message}</p>}
  </Drawer>;
}
