import { useCallback, useEffect, useId, useRef, useState } from "react";
import { Button, Select } from "tdesign-react";
import { api, errorMessage, runExclusive } from "./flow";
import { ModelConnections } from "./ModelConnections";
import { presetConfig, replaceStage, setTextParameter, roleLabels, roles, roleStage, bailianSpeechModels, type Connection, type ModelConfig, type ModelOptions, type Role, type Stage } from "./modelConfig";

type Candidate = { id: string; selectable: boolean; adapter_implemented: boolean; status: string; verification: string; offline_time: string | null; model_info?: { max_output_tokens?: number } };
type Catalog = { items: Candidate[]; next_cursor: string | null; source: string; catalog_time: string | null; total: number; warnings: string[] };
export function ModelSettings({ value, options, concurrency, onChange, onConcurrency, voice = { kind: "default" }, portraitPath, concurrencyEditable = true, onConnectionsChange }: {
  value: ModelConfig; options?: ModelOptions; concurrency: number; onChange: (value: ModelConfig) => void; onConcurrency: (value: number) => void;
  voice?: unknown; portraitPath?: string;
  concurrencyEditable?: boolean;
  onConnectionsChange?: (items: Connection[]) => void;
}) {
  const prefix = useId();
  const [connections, setConnections] = useState<Connection[]>([]);
  const [drawer, setDrawer] = useState(false);
  const [message, setMessage] = useState("");
  const [role, setRole] = useState<Role>("text");
  const [catalogs, setCatalogs] = useState<Record<string, Catalog>>({});
  const [includeRetiring, setIncludeRetiring] = useState(false);
  const [busy, setBusy] = useState(false);
  const [checking, setChecking] = useState(false);
  const version = useRef(0);
  const lock = useRef(false);
  const voiceKey = JSON.stringify(voice);
  const loadConnections = useCallback(async () => {
    const data = await api.get("/api/model-connections"); setConnections(data.items); onConnectionsChange?.(data.items);
    version.current++; setCatalogs({}); setMessage("");
  }, [onConnectionsChange]);
  useEffect(() => { void loadConnections().catch((error) => setMessage(errorMessage(error))); }, [loadConnections]);
  useEffect(() => { version.current++; setBusy(false); setMessage(""); return () => { version.current++; }; }, [value, role, includeRetiring, voiceKey, portraitPath]);
  const stage = roleStage(value, role);
  const catalogKey = `${stage.connection_id}|${role}|${includeRetiring}`;
  const catalog = catalogs[catalogKey];
  const fetchCatalog = async (refresh: boolean, append = false) => {
    const current = ++version.current;
    setBusy(true); setMessage("");
    try {
      const { data } = await api.post("/api/models", { connection_id: stage.connection_id, role,
        include_retiring: includeRetiring, limit: 100, refresh, ...(append ? { cursor: catalog?.next_cursor } : {}) });
      if (current === version.current) setCatalogs((previous) => ({ ...previous,
        [catalogKey]: { ...data, items: append ? [...(catalog?.items ?? []), ...data.items] : data.items } }));
    } catch (error) { if (current === version.current) setMessage(errorMessage(error)); }
    finally { if (current === version.current) setBusy(false); }
  };
  const applyPreset = (preset: ModelConfig["preset"]) => {
    setMessage("");
    if (preset === "custom") { onChange({ ...value, preset }); return; }
    const connection = connections.find((entry) => entry.id === value.stages.text.connection_id && entry.provider === "bailian")
      ?? connections.find((entry) => entry.provider === "bailian");
    onChange(presetConfig(preset, connection?.id ?? "", value.avatar.enabled, options));
  };
  const changeMode = (target: Role, mode: Stage["mode"]) => {
    const current = roleStage(value, target);
    const defaults = presetConfig(mode === "api" ? "bailian_api" : "local_existing", "", value.avatar.enabled, options);
    let next = roleStage(defaults, target);
    if (mode === "local" && (target === "text" || target === "vision")) next = { mode, provider: "openai_compatible", model: current.model, parameters: {} };
    const connection = connections.find((entry) => entry.provider === next.provider);
    onChange(replaceStage(value, target, { ...next, ...(mode === "api" || target === "text" || target === "vision" ? { connection_id: connection?.id } : {}) }));
  };
  const check = (full = false) => {
    if (lock.current) return;
    const current = ++version.current;
    return runExclusive(lock, setChecking, async () => {
    setMessage("");
    const { data } = await api.post("/api/model-config/validate", {
      model_config: value, requested_stages: full ? ["1", "2", "3"] : ["1", "2"], review_before_render: !full, voice, portrait_path: portraitPath,
    });
    if (current === version.current) setMessage(`${full ? "全链路" : "准备阶段"}配置合法：${data.required_roles.map((item: Role) => roleLabels[item]).join("、")}。未验证权限/余额/效果，未进行运行验收。`);
    }, (error) => { if (current === version.current) setMessage(errorMessage(error)); });
  };
  const connectedRoles = roles.filter((item) => roleStage(value, item).connection_id);
  return <div className="model-settings">
    <div className="actions model-presets" aria-label="模型预设">
      {([["bailian_api", "百炼全 API"], ["local_existing", "保留现有本地推理"], ["custom", "自定义组合"]] as const).map(([preset, label]) =>
        <Button key={preset} variant={value.preset === preset ? "base" : "outline"} theme={value.preset === preset ? "primary" : "default"}
          aria-pressed={value.preset === preset} onClick={() => applyPreset(preset)}>{label}</Button>)}
    </div>
    <p className="hint">切换预设只修改模型，保留材料、语言与课程。百炼配音已接入，需选择兼容云音色；API光标定位及数字人仍待P5/P6，全API预设尚不能运行完整视频。</p>
    {options && !options.recommended_models && <p className="notice warn">后端尚未加载P2版本，请自行重载服务。页面不会改用旧目录接口或推理自检绕过。</p>}
    <div className="actions"><Button variant="outline" onClick={() => setDrawer(true)}>管理连接</Button>
      <Button variant="outline" loading={checking} disabled={!options?.recommended_models || busy || checking} onClick={() => check()}>检查准备阶段配置</Button>
      <Button variant="text" disabled={!options?.recommended_models || busy || checking} onClick={() => check(true)}>检查全链路配置</Button></div>
    {connectedRoles.some((item) => !connections.some((entry) => entry.id === roleStage(value, item).connection_id)) && <p className="notice warn">部分连接尚未配置或已失效。请在下表重新选择；历史任务请使用独立的重新绑定入口，不会借用草稿密钥。</p>}
    <div className="model-role-list">
      {roles.filter((item) => item !== "avatar" || value.avatar.enabled).map((item) => {
        const selected = roleStage(value, item);
        const needsConnection = selected.mode === "api" || item === "text" || item === "vision";
        const implemented = item === "text" || item === "vision" ? options?.implemented.compatible_text_vision
          : item === 'tts' && selected.mode === 'api' ? options?.implemented.bailian_speech
          : selected.mode === "local" && item !== "avatar" && options?.implemented.local_tts_asr_grounding;
        const candidates = catalogs[`${selected.connection_id}|${item}|${includeRetiring}`]?.items ?? [];
        const currentCandidate = candidates.find((candidate) => candidate.id === selected.model);
        const choices = candidates.map((candidate) => ({ value: candidate.id,
          label: `${candidate.id}${!candidate.adapter_implemented ? " · 待接入" : candidate.verification === "capability_unknown" ? " · 能力未验证" : ""}${candidate.offline_time ? ` · 下线 ${candidate.offline_time}` : ""}`,
          disabled: !candidate.selectable }));
        if (item === 'tts' && selected.mode === 'api' && options?.implemented.bailian_speech) {
          for (const model of bailianSpeechModels) if (!choices.some((choice) => choice.value === model)) choices.push({ value: model, label: `${model} · 已接入 / 权限未验证`, disabled: false });
        }
        if (!choices.some((choice) => choice.value === selected.model)) choices.unshift({ value: selected.model, label: `${selected.model} · ${needsConnection ? "未验证 / 预设" : "本地"}`, disabled: !implemented });
        return <div className="model-role-row" key={item}>
          <div><strong>{roleLabels[item]}</strong><span className="hint">{item === "asr" ? "缺参考文本时按需使用" : item === "text" || item === "vision" ? "准备阶段使用" : "审核后使用"}</span></div>
          <div className="field"><label htmlFor={`${prefix}-mode-${item}`}>运行方式</label><select id={`${prefix}-mode-${item}`} value={selected.mode} onChange={(event) => changeMode(item, event.target.value as Stage["mode"])}><option value="api">调用 API</option><option value="local">本地{item === "text" || item === "vision" ? "兼容服务" : "推理"}</option></select></div>
          <div className="field"><label htmlFor={`${prefix}-connection-${item}`}>连接</label>{needsConnection ? <select id={`${prefix}-connection-${item}`} value={selected.connection_id ?? ""} onChange={(event) => {
            const connection = connections.find((entry) => entry.id === event.target.value);
            onChange(replaceStage(value, item, { ...selected, connection_id: event.target.value, provider: connection?.provider ?? selected.provider }));
          }}><option value="">请选择连接</option>{connections.filter((entry) => selected.mode === "local" ? entry.provider === "openai_compatible" : item === "text" || item === "vision" || entry.provider === "bailian")
            .map((entry) => <option value={entry.id} key={entry.id}>{entry.label}</option>)}</select> : <span className="hint">无需云连接</span>}</div>
          <div className="field" ref={(element) => element?.querySelector('input')?.setAttribute('id', `${prefix}-model-${item}`)}><label htmlFor={`${prefix}-model-${item}`}>型号</label><Select value={selected.model} filterable creatable={item === "text" || item === "vision"} options={choices}
            popupProps={{ overlayStyle: { maxWidth: "calc(100vw - 32px)" } }}
            onChange={(model) => onChange(replaceStage(value, item, { ...selected, model: String(model) }))}
            onCreate={(model) => onChange(replaceStage(value, item, { ...selected, model: String(model).trim() }))} />
            <span className="hint">{!implemented ? "适配器待接入，不会回退本地" : currentCandidate?.status === "retired" ? "已下线，禁止新执行" : needsConnection ? item === 'tts' ? "原生HTTP流式配音，需云音色及原生端点" : "兼容协议已接入，服务/权限未验证" : "已有本地路线"}</span></div>
          {item === 'tts' && selected.mode === 'api' && <div className="field"><label htmlFor={`${prefix}-rate`}>配音语速（0.5～2）</label><input id={`${prefix}-rate`} type="number" min={0.5} max={2} step={0.1} value={selected.parameters.speech_rate ?? 1} onChange={(event) => onChange(replaceStage(value, item, { ...selected, parameters: { speech_rate: event.target.valueAsNumber } }))} /></div>}
          {(item === "text" || item === "vision") && <details className="model-role-parameters"><summary>请求参数 · {selected.parameters.max_output_tokens == null && selected.parameters.temperature == null ? "未覆盖" : "已自定义"}</summary>
            <div className="model-parameter-fields">
              <div className="field"><label htmlFor={`${prefix}-tokens-${item}`}>最大输出 tokens</label><input id={`${prefix}-tokens-${item}`} type="number" min={1} max={Math.min(131072, currentCandidate?.model_info?.max_output_tokens ?? 131072)} step={1} placeholder="不覆盖服务默认值" disabled={!options?.implemented.role_model_parameters} value={selected.parameters.max_output_tokens ?? ""}
                onChange={(event) => onChange(setTextParameter(value, item, "max_output_tokens", event.target.value === "" ? undefined : event.target.valueAsNumber))} /></div>
              <div className="field"><label htmlFor={`${prefix}-temperature-${item}`}>温度 temperature</label><input id={`${prefix}-temperature-${item}`} type="number" min={0} max={2} step={0.1} placeholder="不覆盖默认温度" disabled={!options?.implemented.role_model_parameters} value={selected.parameters.temperature ?? ""}
                onChange={(event) => onChange(setTextParameter(value, item, "temperature", event.target.value === "" ? undefined : event.target.valueAsNumber))} /></div>
            </div><p className="hint">仅影响此角色；清空取消覆盖。范围：输出1～131072、温度0～2；已知型号还受目录上限约束，未知兼容服务以其文档为准。v2不借用旧任务全局参数。推荐百炼型号使用非思考模式输出课件/讲稿。</p>
            {!options?.implemented.role_model_parameters && <p className="notice warn">后端尚未支持逐角色参数，请重载至P3版本。</p>}
          </details>}
        </div>;
      })}
    </div>
    <p className="hint">时间轴：{value.timing.source === "tts_timestamp" ? "随 API 配音返回，不加载本地对齐器" : "本地 ForcedAligner"}。文字 / 视觉的本地模式需要自行部署兼容服务，不自动加载权重。{options?.implemented.independent_text_vision_connections ? "文字和视觉可分别选择本地/API连接，任务按角色独立调用。" : "当前后端仍要求文字和视觉共用连接，请重载至P3版本。"}</p>
    <p className={value.avatar.enabled ? "notice warn" : "hint"}>{value.avatar.enabled
      ? "数字人单段音频必须小于20秒。计划长讲解自动分段拼接，不限制整页/成片；分段适配尚待P6，当前v2数字人执行禁用。"
      : "未启用数字人，不施加20秒分段限制。"}</p>
    <details><summary>按环节查询目录</summary><div className="actions">
      <label htmlFor={`${prefix}-catalog-role`}>环节</label><select id={`${prefix}-catalog-role`} value={role} onChange={(event) => setRole(event.target.value as Role)}>{roles.map((item) => <option key={item} value={item}>{roleLabels[item]}</option>)}</select>
      <label className="checkline"><input type="checkbox" checked={includeRetiring} onChange={(event) => setIncludeRetiring(event.target.checked)} />含下线/将下线型号</label>
      <Button variant="outline" disabled={!options?.recommended_models || !stage.connection_id || busy} onClick={() => fetchCatalog(false)}>查看缓存 / 快照</Button>
      <Button variant="outline" loading={busy} disabled={!options?.recommended_models || !stage.connection_id || busy} onClick={() => fetchCatalog(true)}>刷新目录（仅 GET）</Button>
    </div>{catalog && <><p className="hint">{catalog.source} · {catalog.catalog_time ?? "未获取"} · 已显示 {catalog.items.length}/{catalog.total}。{catalog.warnings.join(" ")}</p>
      {catalog.next_cursor && <Button variant="text" disabled={busy} onClick={() => fetchCatalog(false, true)}>加载更多</Button>}</>}</details>
    {message && <p className="notice info" role="status">{message}</p>}
    <div className="field"><label htmlFor={`${prefix}-concurrency`}>讲稿并发请求数（1～8，不控制本地 GPU / 数字人）</label><input id={`${prefix}-concurrency`} type="number" min={1} max={8} step={1} value={concurrency} readOnly={!concurrencyEditable} onChange={(event) => onConcurrency(Number(event.target.value))} /></div>
    <ModelConnections visible={drawer} connections={connections} onClose={() => setDrawer(false)} onReload={loadConnections} />
  </div>;
}
