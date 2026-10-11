import { useId } from "react";
import { defaultSubtitleStyle, subtitleBackground, type SubtitleStyle } from "./subtitleStyle";
import { Section } from "./Section";

export function SubtitleSettings({ value, onChange, disabled = false }: {
  value: SubtitleStyle; onChange: (style: SubtitleStyle) => void; disabled?: boolean;
}) {
  const id = useId();
  const update = (change: Partial<SubtitleStyle>) => onChange({ ...value, ...change });
  const sliders = [
    ["font_size", "字体大小（1080p基准）", 16, 72, 1, `${value.font_size} px`],
    ["line_spacing", "字幕行距", 1.6, 2.4, 0.1, `${value.line_spacing.toFixed(1)} 倍`],
    ["background_opacity", "背景不透明度", 0, 1, 0.05, `${Math.round(value.background_opacity * 100)}%`],
    ["margin_horizontal", "左右安全边距", 2, 20, 1, `${value.margin_horizontal}%`],
    ["margin_bottom", "距底部", 1, 15, 1, `${value.margin_bottom}%`],
  ] as const;
  return <Section title="字幕样式" compact summary={value.enabled ? "自动换行 · 可自定义" : "不烧录字幕"}
    help="字幕始终自动换行，字号随成片高度缩放。只影响合片，不改变讲稿、配音或时间轴。字体以服务端安装情况为准，预览仅作示意。">
    <fieldset className="subtitle-settings" disabled={disabled}>
      <legend className="sr-only">字幕自定义配置</legend>
      <div className="actions"><label className="checkrow"><input type="checkbox" checked={value.enabled} onChange={(event) => update({ enabled: event.target.checked })} />烧录字幕</label>
        <button className="subtitle-reset" type="button" onClick={() => onChange({ ...defaultSubtitleStyle })}>恢复默认</button></div>
      <div className="subtitle-settings-layout">
        <div className="subtitle-controls">
          <div className="field"><label htmlFor={id + '-font'}>字体</label>
            <input id={id + '-font'} list={id + '-fonts'} autoComplete="off" maxLength={80} value={value.font_name} disabled={!value.enabled} placeholder="自动 · 跟随讲稿语言" onChange={(event) => {
              if (!/[\x00-\x1f,\\{}\/]|^[a-z][a-z0-9+.-]*:/i.test(event.target.value.trim())) update({ font_name: event.target.value });
            }} />
            <datalist id={id + '-fonts'}>{["Microsoft YaHei", "SimHei", "SimSun", "Noto Sans CJK SC", "Arial", "DejaVu Sans"].map((font) => <option key={font} value={font} />)}</datalist>
          </div>
          <div className="subtitle-colors">{[["text_color", "文字颜色"], ["background_color", "背景颜色"]].map(([key, label]) => <div className="field" key={key}>
            <label htmlFor={id + key}>{label}</label><div className="subtitle-color-input"><input id={id + key} type="color" disabled={!value.enabled} value={value[key as 'text_color' | 'background_color']} onChange={(event) => update({ [key]: event.target.value })} /><output>{value[key as 'text_color' | 'background_color']}</output></div>
          </div>)}</div>
          {sliders.map(([key, label, min, max, step, display]) => <div className="field" key={key}>
            <label htmlFor={id + key}>{label}<output htmlFor={id + key}>{display}</output></label>
            <input id={id + key} type="range" min={min} max={max} step={step} value={value[key]} disabled={!value.enabled} onChange={(event) => update({ [key]: Number(event.target.value) })} />
          </div>)}
        </div>
        <div className="subtitle-preview-column">
          <div className="subtitle-preview" aria-label="字幕样式示意预览">
            <div className="subtitle-preview-slide"><strong>自然语言处理基础</strong><p>从文本分析到语言理解</p></div>
            {value.enabled ? <div className="subtitle-preview-caption" style={{ left: `${value.margin_horizontal}%`, right: `${value.margin_horizontal}%`, bottom: `${value.margin_bottom}%`, fontSize: `${value.font_size / 9.6}cqw`, lineHeight: value.line_spacing, fontFamily: value.font_name || "inherit", color: value.text_color }}>
              <span style={{ background: subtitleBackground(value), textShadow: value.background_opacity === 0 ? "0 1px 2px #000, 0 -1px 2px #000" : undefined }}>自然语言处理连接人类语言与计算机，较长字幕会在安全区域内自动换行。</span>
            </div> : <div className="subtitle-preview-off">字幕已关闭</div>}
          </div>
          <p className="hint">预览为样式示意，字体以服务端为准。行距应用于实际合片；背景设为0%可去掉底色。长字幕按真实字词时间切分，最多两行。</p>
        </div>
      </div>
    </fieldset>
  </Section>;
}
