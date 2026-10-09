import { useCallback, useEffect, useId, useRef, useState } from "react";
import { Button, Select } from "tdesign-react";
import { api, errorMessage, runExclusive } from "./flow";
import { ModelConnections } from "./ModelConnections";
import { Section } from "./Section";
import { ModelPrice, ModelPriceProvider, type CatalogModel } from './ModelPrice';
import { presetConfig, replaceStage, setTextParameter, roleLabels, roles, roleStage, bailianSpeechModels, type Connection, type ModelConfig, type ModelOptions, type Role, type Stage } from "./modelConfig";

type Catalog = { items: CatalogModel[]; next_cursor: string | null; source: string; catalog_time: string | null; total: number; warnings: string[] };
export function ModelSettings({ value, options, concurrency, onChange, onConcurrency, voice = { kind: "default" }, portraitPath, preparePortrait, portraitKey, concurrencyEditable = true, onConnectionsChange }: {
  value: ModelConfig; options?: ModelOptions; concurrency: number; onChange: (value: ModelConfig) => void; onConcurrency: (value: number) => void;
  voice?: unknown; portraitPath?: string;
  preparePortrait?: () => Promise<string | undefined>; portraitKey?: unknown;
  concurrencyEditable?: boolean;
  onConnectionsChange?: (items: Connection[]) => void;
}) {
  const prefix = useId();
  const [connections, setConnections] = useState<Connection[]>([]);
  const [drawer, setDrawer] = useState(false);
  const [message, setMessage] = useState("");
  const [catalogs, setCatalogs] = useState<Record<string, Catalog>>({});
  const [catalogReload, setCatalogReload] = useState(0);
  const [modelSearch, setModelSearch] = useState('');
  const [openPicker, setOpenPicker] = useState<Role>();
  const [includeRetiring, setIncludeRetiring] = useState(false);
  const [busy, setBusy] = useState(false);
  const [checking, setChecking] = useState(false);
  const [checkingFull, setCheckingFull] = useState(false);
  const [checkMessage, setCheckMessage] = useState("");
  const [checkFailed, setCheckFailed] = useState(false);
  const checkVersion = useRef(0);
  const lock = useRef(false);
  const voiceKey = JSON.stringify(voice);
  const loadConnections = useCallback(async () => {
    const data = await api.get("/api/model-connections"); setConnections(data.items); onConnectionsChange?.(data.items);
    setCatalogs({}); setMessage("");
  }, [onConnectionsChange]);
  useEffect(() => { void loadConnections().catch((error) => setMessage(errorMessage(error))); }, [loadConnections]);
  useEffect(() => { checkVersion.current++; setCheckMessage(""); return () => { checkVersion.current++; }; }, [value, voiceKey, portraitPath, portraitKey, connections]);
  const connectionKey = connections.map((entry) => entry.id).sort().join('|');
  useEffect(() => {
    let active = true;
    setBusy(true); setMessage('');
    void Promise.all(connections.map(async (connection) => {
      let result: Catalog | undefined;
      do {
        const { data } = await api.post('/api/models', { connection_id: connection.id, role: 'all', include_retiring: true,
          limit: 100, refresh: !result && catalogReload > 0, ...(result?.next_cursor ? { cursor: result.next_cursor } : {}) });
        result = { ...data, items: [...(result?.items ?? []), ...data.items] };
      } while (result?.next_cursor && active);
      if (active && result) setCatalogs((previous) => ({ ...previous, [connection.id]: result! }));
    })).catch((error) => { if (active) setMessage(errorMessage(error)); })
      .finally(() => { if (active) setBusy(false); });
    return () => { active = false; };
  }, [connectionKey, catalogReload]);
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
    const current = ++checkVersion.current;
    return runExclusive(lock, setChecking, async () => {
    setCheckingFull(full);
    setCheckFailed(false); setCheckMessage(`正在检查${full ? "全链路" : "准备阶段"}配置…`);
    const problems = roles.filter((item) => item !== "avatar" || value.avatar.enabled).flatMap((item) => {
      const selected = roleStage(value, item);
      if (selected.mode !== "api" && item !== "text" && item !== "vision") return [];
      const connection = connections.find((entry) => entry.id === selected.connection_id);
      return !connection ? [`${roleLabels[item]}：请选择有效连接`] : selected.mode === "api" && !connection.key_present ? [`${roleLabels[item]}：请补充连接密钥`] : [];
    });
    if (value.stages.tts.mode === "api" && !(voice as { resource_id?: string })?.resource_id) problems.push("音色：请选择与配音型号匹配的云音色");
    if (problems.length) throw new Error(`${full ? "全链路" : "准备阶段"}配置还需完善：${problems.join("；")}`);
    const checkedPortrait = value.avatar.enabled && preparePortrait ? await preparePortrait() : portraitPath;
    if (current !== checkVersion.current) return;
    if (value.avatar.enabled && !checkedPortrait) throw new Error("数字人：请先选择人像，或关闭数字人");
    const { data } = await api.post("/api/model-config/validate", {
      model_config: value, requested_stages: full ? ["1", "2", "3"] : ["1", "2"], review_before_render: !full, voice, portrait_path: checkedPortrait,
    });
    if (current === checkVersion.current) setCheckMessage(`${full ? "全链路" : "准备阶段"}配置检查通过：${data.required_roles.map((item: Role) => roleLabels[item]).join("、")}。此检查不调用模型，调用权限、余额和效果需实际运行确认。`);
    }, (error) => { if (current === checkVersion.current) { setCheckFailed(true); setCheckMessage(errorMessage(error)); } });
  };
  const connectedRoles = roles.filter((item) => roleStage(value, item).connection_id);
  return <ModelPriceProvider><div className="model-settings">
    <div className="actions model-presets" aria-label="模型预设">
      {([["bailian_api", "百炼全 API"], ["local_existing", "保留现有本地推理"], ["custom", "自定义组合"]] as const).map(([preset, label]) =>
        <Button key={preset} variant={value.preset === preset ? "base" : "outline"} theme={value.preset === preset ? "primary" : "default"}
          aria-pressed={value.preset === preset} onClick={() => applyPreset(preset)}>{label}</Button>)}
    </div>
    <p className="hint">选择整体方案，再展开需要调整的环节。切换方案保留素材、语言与课程设置。</p>
    <div className="model-catalog-toolbar"><span role="status" aria-live="polite">{busy ? '正在同步模型及费用…' : connections.length ? `已加载 ${Object.values(catalogs).reduce((total, catalog) => total + catalog.items.length, 0)} 个模型记录 · 按环节筛选` : '添加连接后自动获取模型及费用'}</span>
      <Button variant="text" loading={busy} disabled={!connections.length || busy} onClick={() => setCatalogReload((count) => count + 1)}>刷新模型与费用</Button>
      <label className="checkline"><input type="checkbox" checked={includeRetiring} onChange={(event) => setIncludeRetiring(event.target.checked)} />显示将下线 / 已下线</label></div>
    {options && !options.recommended_models && <p className="notice warn">后端尚未加载P2版本，请自行重载服务。页面不会改用旧目录接口或推理自检绕过。</p>}
    <div className="actions"><Button variant="outline" onClick={() => setDrawer(true)}>管理连接</Button>
      <Button variant="outline" loading={checking && !checkingFull} disabled={!options?.recommended_models || busy || checking} onClick={() => check()}>检查准备阶段配置</Button>
      <Button variant="text" loading={checking && checkingFull} disabled={!options?.recommended_models || busy || checking} onClick={() => check(true)}>检查全链路配置</Button></div>
    {checkMessage && <p className={`notice ${checkFailed ? "warn" : "info"}`} role="status" aria-live="polite">{checkMessage}</p>}
    {connectedRoles.some((item) => !connections.some((entry) => entry.id === roleStage(value, item).connection_id)) && <p className="notice warn">部分连接尚未配置或已失效。请在下表重新选择；历史任务请使用独立的重新绑定入口，不会借用草稿密钥。</p>}
    <div className="model-role-list">
      {roles.filter((item) => item !== "avatar" || value.avatar.enabled).map((item) => {
        const selected = roleStage(value, item);
        const needsConnection = selected.mode === "api" || item === "text" || item === "vision";
        const implemented = item === "text" || item === "vision" ? options?.implemented.compatible_text_vision
          : item === 'tts' && selected.mode === 'api' ? options?.implemented.bailian_speech
          : item === 'grounding' && selected.mode === 'api' ? options?.implemented.bailian_grounding
          : item === 'avatar' ? selected.mode === 'api' ? options?.implemented.bailian_avatar : options?.implemented.avatar_segmentation
          : selected.mode === "local" && options?.implemented.local_tts_asr_grounding;
        const connectionCatalog = catalogs[selected.connection_id ?? ''];
        const allCandidates = selected.mode === 'api' || item === 'text' || item === 'vision'
          ? (connectionCatalog?.items ?? []).filter((candidate) => candidate.roles.includes(item)).map((candidate) => ({ ...candidate, ...candidate.role_support[item] })) : [];
        const candidates = allCandidates.filter((candidate) => includeRetiring || candidate.status === 'active');
        const currentCandidate = allCandidates.find((candidate) => candidate.id === selected.model);
        const choices = candidates.map((candidate) => ({ value: candidate.id,
          label: `${candidate.id}${!candidate.adapter_implemented ? " · 待接入" : candidate.verification === "capability_unknown" ? " · 能力未验证" : ""}${candidate.offline_time ? ` · 下线 ${candidate.offline_time}` : ""}`,
          content: <ModelPrice model={candidate.id} pricing={candidate.pricing}><span className="model-option-name">{candidate.id}{!candidate.adapter_implemented ? ' · 待接入' : candidate.verification === 'capability_unknown' ? ' · 能力未验证' : ''}{candidate.offline_time ? ` · 下线 ${candidate.offline_time}` : ''}</span></ModelPrice>,
          disabled: !candidate.selectable }));
        if (!connectionCatalog?.items.length && item === 'tts' && selected.mode === 'api' && options?.implemented.bailian_speech) {
          for (const model of bailianSpeechModels) if (!choices.some((choice) => choice.value === model)) choices.push({ value: model, label: model, content: <ModelPrice model={model}><span>{model}</span></ModelPrice>, disabled: false });
        }
        if (!choices.some((choice) => choice.value === selected.model)) choices.unshift({ value: selected.model, label: selected.model,
          content: <ModelPrice model={selected.model} pricing={currentCandidate?.pricing}><span>{selected.model}</span></ModelPrice>, disabled: !implemented || (currentCandidate ? !currentCandidate.selectable : false) });
        const connectionLabel = connections.find((entry) => entry.id === selected.connection_id)?.label ?? "未选择连接";
        return <Section title={roleLabels[item]} key={item} compact defaultOpen={false}
          summary={`${selected.mode === "api" ? "API" : "本地"} · ${selected.model} · ${needsConnection ? connectionLabel : "无需连接"}`}>
          <div className="model-role-row">
          <div><strong>{roleLabels[item]}</strong><span className="hint">{item === "asr" ? "缺参考文本时按需使用" : item === "text" || item === "vision" ? "准备阶段使用" : "审核后使用"}</span></div>
          <div className="field"><label htmlFor={`${prefix}-mode-${item}`}>运行方式</label><select id={`${prefix}-mode-${item}`} value={selected.mode} onChange={(event) => changeMode(item, event.target.value as Stage["mode"])}><option value="api">调用 API</option><option value="local">本地{item === "text" || item === "vision" ? "兼容服务" : "推理"}</option></select></div>
          <div className="field"><label htmlFor={`${prefix}-connection-${item}`}>连接</label>{needsConnection ? <select id={`${prefix}-connection-${item}`} value={selected.connection_id ?? ""} onChange={(event) => {
            const connection = connections.find((entry) => entry.id === event.target.value);
            onChange(replaceStage(value, item, { ...selected, connection_id: event.target.value, provider: connection?.provider ?? selected.provider }));
          }}><option value="">请选择连接</option>{connections.filter((entry) => selected.mode === "local" ? entry.provider === "openai_compatible" : item === "text" || item === "vision" || entry.provider === "bailian")
            .map((entry) => <option value={entry.id} key={entry.id}>{entry.label}</option>)}</select> : <span className="hint">无需云连接</span>}</div>
          <div className="field" ref={(element) => element?.querySelector('input')?.setAttribute('id', `${prefix}-model-${item}`)}><label htmlFor={`${prefix}-model-${item}`}>型号 · {candidates.filter((candidate) => candidate.selectable).length} 个可选</label>
            <Select value={selected.model} filterable creatable={item === "text" || item === "vision"} options={choices}
            onPopupVisibleChange={(visible) => setOpenPicker(visible ? item : undefined)}
            popupProps={{ overlayStyle: { maxWidth: "calc(100vw - 32px)" } }}
            onChange={(model) => onChange(replaceStage(value, item, { ...selected, model: String(model) }))}
            onCreate={(model) => onChange(replaceStage(value, item, { ...selected, model: String(model).trim() }))} />
            <ModelPrice model={selected.model} pricing={currentCandidate?.pricing} triggerElement={`[id="${prefix}-model-${item}"]`} disabled={openPicker === item || selected.mode === 'local'}>{null}</ModelPrice>
            {selected.mode === 'api' && <ModelPrice model={selected.model} pricing={currentCandidate?.pricing}><button type="button" className="model-price-link">费用明细</button></ModelPrice>}
            <span className="hint">{!implemented ? "适配器待接入，不会回退本地" : currentCandidate?.status === "retired" ? "已下线，禁止新执行" : needsConnection ? item === 'tts' ? "原生HTTP流式配音，需云音色及原生端点" : item === 'avatar' ? "Wan检测与异步生成已接入，权限和效果未验证" : "兼容协议已接入，服务/权限未验证" : "已有本地路线"}</span></div>
          {needsConnection && !currentCandidate && <p className="hint model-role-parameters">{busy ? '正在查询当前连接的目录。' : '此型号未在当前连接的对应环节目录中匹配；可刷新目录核对。'}配置检查通过也不代表已获得调用权限。</p>}
          {item === 'tts' && selected.mode === 'api' && <div className="field"><label htmlFor={`${prefix}-rate`}>配音语速（0.5～2）</label><input id={`${prefix}-rate`} type="number" min={0.5} max={2} step={0.1} value={selected.parameters.speech_rate ?? 1} onChange={(event) => onChange(replaceStage(value, item, { ...selected, parameters: { speech_rate: event.target.valueAsNumber } }))} /></div>}
          {item === 'avatar' && selected.mode === 'api' && <div className="field"><label htmlFor={`${prefix}-resolution`}>数字人输出分辨率</label><select id={`${prefix}-resolution`} value={selected.parameters.resolution ?? '480P'} onChange={(event) => onChange(replaceStage(value, item, { ...selected, parameters: { resolution: event.target.value as '480P' | '720P' } }))}><option value="480P">480P</option><option value="720P">720P</option></select><span className="hint">按生成视频时长与此分辨率估算费用；云端片段串行提交。</span></div>}
          {(item === "text" || item === "vision") && <details className="model-role-parameters"><summary>请求参数 · {selected.parameters.max_output_tokens == null && selected.parameters.temperature == null ? "未覆盖" : "已自定义"}</summary>
            <div className="model-parameter-fields">
              <div className="field"><label htmlFor={`${prefix}-tokens-${item}`}>最大输出 tokens</label><input id={`${prefix}-tokens-${item}`} type="number" min={1} max={Math.min(131072, currentCandidate?.model_info?.max_output_tokens ?? 131072)} step={1} placeholder="不覆盖服务默认值" disabled={!options?.implemented.role_model_parameters} value={selected.parameters.max_output_tokens ?? ""}
                onChange={(event) => onChange(setTextParameter(value, item, "max_output_tokens", event.target.value === "" ? undefined : event.target.valueAsNumber))} /></div>
              <div className="field"><label htmlFor={`${prefix}-temperature-${item}`}>温度 temperature</label><input id={`${prefix}-temperature-${item}`} type="number" min={0} max={2} step={0.1} placeholder="不覆盖默认温度" disabled={!options?.implemented.role_model_parameters} value={selected.parameters.temperature ?? ""}
                onChange={(event) => onChange(setTextParameter(value, item, "temperature", event.target.value === "" ? undefined : event.target.valueAsNumber))} /></div>
            </div><p className="hint">仅影响此角色；清空取消覆盖。范围：输出1～131072、温度0～2；已知型号还受目录上限约束，未知兼容服务以其文档为准。v2不借用旧任务全局参数。推荐百炼型号使用非思考模式输出课件/讲稿。</p>
            {!options?.implemented.role_model_parameters && <p className="notice warn">后端尚未支持逐角色参数，请重载至P3版本。</p>}
          </details>}
          </div>
        </Section>;
      })}
    </div>
    <details><summary>运行说明 · {value.timing.source === "tts_timestamp" ? "云端时间轴" : "本地时间轴"}{value.avatar.enabled ? " · 数字人自动分段" : ""}</summary>
      <p className="hint">时间轴：{value.timing.source === "tts_timestamp" ? "随 API 配音返回，不加载本地对齐器" : "本地 ForcedAligner"}。文字 / 视觉的本地模式需要自行部署兼容服务，不自动加载权重。{options?.implemented.independent_text_vision_connections ? "文字和视觉可分别选择本地/API连接，任务按角色独立调用。" : "当前后端仍要求文字和视觉共用连接，请重载至P3版本。"}</p>
      <p className={value.avatar.enabled ? "notice warn" : "hint"}>{value.avatar.enabled
        ? "数字人单段音频严格小于20秒，默认约18秒自动分段并用完整页音轨拼接，不限制整页/成片。本地MuseTalk与百炼Wan已接入分段；Wan真实素材与生成效果仍待授权验收。"
        : "未启用数字人，不施加20秒分段限制。"}</p>
    </details>
    <details><summary>全部模型目录与查询记录</summary>
      <div className="field"><label htmlFor={`${prefix}-model-search`}>搜索全部型号</label><input id={`${prefix}-model-search`} value={modelSearch} placeholder="输入模型名称，如 qwen、cosyvoice" onChange={(event) => setModelSearch(event.target.value)} /></div>
      {connections.map((connection) => { const catalog = catalogs[connection.id]; return <div key={connection.id} className="model-catalog-record">
        <strong>{connection.label} · {catalog?.items.length ?? 0} 个模型</strong>
        <p className="hint">{catalog?.catalog_time ? new Date(catalog.catalog_time).toLocaleString('zh-CN', { hour12: false }) : '尚未获得目录'} · {catalog?.source === 'workspace_snapshot' ? '历史快照' : catalog?.source === 'live_directory' ? '官方接口目录' : '暂无目录'}。{catalog?.warnings.join(' ')}</p>
        <div className="model-catalog-list">{catalog?.items.filter((model) => model.id.toLowerCase().includes(modelSearch.toLowerCase())).map((model) => <div key={model.id}>
          <ModelPrice model={model.id} pricing={model.pricing}><button type="button" className="model-price-link">{model.id}</button></ModelPrice>
          <span className="hint">{model.roles.length ? model.roles.map((role) => `${roleLabels[role as Role]}${model.role_support[role]?.adapter_implemented ? '' : '（待接入）'}`).join(' / ') : '未用于当前流程'}{model.offline_time ? ` · 下线 ${model.offline_time}` : ''}</span>
        </div>)}</div></div>; })}
    </details>
    {message && <p className="notice info" role="status">{message}</p>}
    <div className="field"><label htmlFor={`${prefix}-concurrency`}>讲稿并发请求数（1～8，不控制本地 GPU / 数字人）</label><input id={`${prefix}-concurrency`} type="number" min={1} max={8} step={1} value={concurrency} readOnly={!concurrencyEditable} onChange={(event) => onConcurrency(Number(event.target.value))} /></div>
    <ModelConnections visible={drawer} connections={connections} onClose={() => setDrawer(false)} onReload={loadConnections} />
  </div></ModelPriceProvider>;
}
