import { useId, useState } from 'react';
import { Button, Dialog } from 'tdesign-react';
import { Section } from './Section';
import { defaultSlideDesign, slideDensities, slideDesignSummary, slideLayouts, slideThemes, type SlideDesign } from './slideDesign';

type PreviewPage = 'cover' | 'content' | 'summary';
const previewPages = [{ id: 'cover', label: '封面' }, { id: 'content', label: '内容页' }, { id: 'summary', label: '小结' }] as const;

function SlidePreview({ value, page, language }: { value: SlideDesign; page: PreviewPage; language: string }) {
  const theme = slideThemes.find(item => item.id === value.theme)!;
  const wide = value.ratio === 'wide';
  const width = wide ? 960 : 720;
  const center = width / 2;
  const en = language === 'en';
  const count = value.density === 'airy' ? 2 : value.density === 'standard' ? 3 : 4;
  const points = en
    ? ['Start with a clear question', 'Connect concepts to evidence', 'Explain through an example', 'Check understanding together']
    : ['以一个清晰的问题切入', '把概念与证据联系起来', '通过例子理解方法', '用练习检验理解'];
  const fontFamily = value.font === 'serif' ? 'Georgia, SimSun, serif' : 'Arial, Microsoft YaHei, sans-serif';
  const panel = (x: number, y: number, w: number, h: number) => <rect x={x} y={y} width={w} height={h} rx="8" fill={theme.panel} />;
  const bulletList = (x: number, y: number, step: number, size: number) => points.slice(0, count).map((point, i) => <g key={point}>
    <circle cx={x} cy={y + i * step - 6} r="4" fill={theme.accent} /><text x={x + 18} y={y + i * step} fontSize={size}>{point}</text>
  </g>);
  return <svg className="ppt-slide" viewBox={`0 0 ${width} 540`} role="img" aria-label={`${previewPages.find(item => item.id === page)!.label}排版示意：${slideDesignSummary(value)}`} style={{ aspectRatio: wide ? '16 / 9' : '4 / 3', fontFamily }}>
    <rect width={width} height="540" fill={theme.background} />
    <g fill={theme.ink}>
      {page === 'cover' ? <>
        <rect x={center - 32} y="122" width="64" height="5" fill={theme.accent} />
        <text x={center} y="220" textAnchor="middle" fontSize={wide ? 44 : 38} fontWeight="700">{en ? 'From Questions to Understanding' : '从问题出发，理解知识'}</text>
        <text x={center} y="280" textAnchor="middle" fontSize="22">{en ? 'Concepts · Examples · Practice' : '概念梳理 · 示例讲解 · 应用练习'}</text>
        <text x={center} y="400" textAnchor="middle" fontSize="17" fill={theme.accent}>{en ? 'A LEARNING JOURNEY' : '一堂循序渐进的课'}</text>
      </> : page === 'summary' ? <>
        <text x="56" y="82" fontSize="32" fontWeight="700">{en ? 'What we take away' : '回顾：把知识串联起来'}</text>
        <rect x="56" y="105" width="52" height="4" fill={theme.accent} />
        {[en ? 'Understand' : '理解概念', en ? 'Connect' : '建立联系', en ? 'Apply' : '迁移应用'].map((label, i) => {
          const boxWidth = (width - 160) / 3;
          const x = 56 + i * (boxWidth + 24);
          return <g key={label}>{panel(x, 168, boxWidth, 192)}<text x={x + boxWidth / 2} y="225" textAnchor="middle" fontSize="18" fill={theme.accent}>0{i + 1}</text><text x={x + boxWidth / 2} y="284" textAnchor="middle" fontSize="24" fontWeight="700">{label}</text></g>;
        })}
        <text x={center} y="427" textAnchor="middle" fontSize="20">{en ? 'Next: explain the idea in your own words.' : '下一步：尝试用自己的话解释核心观点。'}</text>
      </> : <>
        <text x="56" y="82" fontSize="32" fontWeight="700">{en ? 'Build a clear learning path' : '建立清晰的学习路径'}</text>
        <rect x="56" y="105" width="52" height="4" fill={theme.accent} />
        {value.layout === 'split' ? <>
          {bulletList(62, 195, 68, wide ? 24 : 19)}
          {panel(width * .56, 150, width * .38, 296)}
          {[en ? 'Question' : '问题', en ? 'Concept' : '概念', en ? 'Practice' : '练习'].map((label, i) => <g key={label}>
            {i > 0 && <path d={`M ${width * .75} ${143 + i * 90} v 21 m -5 -5 l 5 5 5 -5`} fill="none" stroke={theme.accent} strokeWidth="2" />}
            <rect x={width * .61} y={178 + i * 90} width={width * .28} height="50" rx="5" fill={theme.background} />
            <text x={width * .75} y={211 + i * 90} textAnchor="middle" fontSize="23">{label}</text>
          </g>)}
        </> : value.layout === 'focus' ? <>
          {panel(72, 150, width - 144, 116)}
          <text x={center} y="222" textAnchor="middle" fontSize="32" fontWeight="700" fill={theme.accent}>{en ? 'Understand → Connect → Apply' : '理解 → 联系 → 应用'}</text>
          {bulletList(wide ? 220 : 146, 320, 40, wide ? 23 : 21)}
        </> : <>
          {[en ? 'Core idea' : '核心概念', en ? 'Supporting example' : '应用示例'].map((label, i) => {
            const boxWidth = (width - 136) / 2;
            const x = 56 + i * (boxWidth + 24);
            return <g key={label}>{panel(x, 150, boxWidth, 296)}
              <text x={x + boxWidth / 2} y="202" textAnchor="middle" fontSize="25" fontWeight="700" fill={theme.accent}>{label}</text>
              {points.slice(0, count).map((point, j) => <text key={point} x={x + 24} y={258 + j * 48} fontSize={wide ? 20 : 15}>{i === 0 ? point : (en ? ['Observe a case', 'Identify the pattern', 'Explain the result', 'Try another case'] : ['观察一个具体情境', '识别其中的规律', '解释得到的结果', '尝试新的应用场景'])[j]}</text>)}
            </g>;
          })}
        </>}
      </>}
      <line x1="56" y1="486" x2={width - 56} y2="486" stroke={theme.accent} opacity=".45" />
      <text x="56" y="514" fontSize="14" fill={theme.accent}>{en ? 'LAYOUT SAMPLE' : '排版示意'}</text><text x={width - 56} y="514" textAnchor="end" fontSize="14">{page === 'cover' ? '01' : page === 'content' ? '02' : '03'}</text>
    </g>
  </svg>;
}

function DesignControls({ value, onChange, includeTheme = false }: { value: SlideDesign; onChange: (value: SlideDesign) => void; includeTheme?: boolean }) {
  const id = useId();
  const update = (change: Partial<SlideDesign>) => onChange({ ...value, ...change });
  return <div className="ppt-controls">
    {includeTheme && <div className="field"><label htmlFor={id + '-theme'}>PPT 风格</label><select id={id + '-theme'} value={value.theme} onChange={event => update({ theme: event.target.value as SlideDesign['theme'] })}>{slideThemes.map(theme => <option key={theme.id} value={theme.id}>{theme.label}</option>)}</select></div>}
    <div className="field"><label htmlFor={id + '-layout'}>内容页布局</label><select id={id + '-layout'} value={value.layout} onChange={event => update({ layout: event.target.value as SlideDesign['layout'] })}>{slideLayouts.map(layout => <option key={layout.id} value={layout.id}>{layout.label}</option>)}</select><small>{slideLayouts.find(layout => layout.id === value.layout)!.description}</small></div>
    <div className="field"><label htmlFor={id + '-density'}>内容疏密</label><select id={id + '-density'} value={value.density} onChange={event => update({ density: event.target.value as SlideDesign['density'] })}>{slideDensities.map(density => <option key={density.id} value={density.id}>{density.label}</option>)}</select></div>
    <div className="ppt-control-pair">
      <div className="field"><label htmlFor={id + '-font'}>字体气质</label><select id={id + '-font'} value={value.font} onChange={event => update({ font: event.target.value as SlideDesign['font'] })}><option value="sans">简洁无衬线</option><option value="serif">典雅衬线</option></select></div>
      <div className="field"><label htmlFor={id + '-ratio'}>画幅</label><select id={id + '-ratio'} value={value.ratio} onChange={event => update({ ratio: event.target.value as SlideDesign['ratio'] })}><option value="wide">16:9 宽屏</option><option value="standard">4:3 标准</option></select></div>
    </div>
  </div>;
}

export function SlideDesignPanel({ value, onChange, language }: { value: SlideDesign; onChange: (value: SlideDesign) => void; language: string }) {
  const id = useId();
  const [page, setPage] = useState<PreviewPage>('content');
  const [expanded, setExpanded] = useState(false);
  const pageButtons = <div className="ppt-page-switch" role="group" aria-label="预览页面类型">{previewPages.map(item => <button type="button" key={item.id} aria-pressed={page === item.id} onClick={() => setPage(item.id)}>{item.label}</button>)}</div>;
  return <Section anchor="slides" number="03" title="PPT 风格与布局" summary={slideDesignSummary(value)} help={<>
    <p>选择配色与内容页布局，再预览封面、内容页和小结。调整即时应用到新任务，并保存在本机草稿中。</p>
    <p>预览采用示例内容，展示视觉方向；实际课件由模型根据素材生成，字体以服务端可用字体为准。生成后可在任务详情逐页审核。</p>
    <p>视觉设置会追加到幻灯片生成提示词，并优先于提示词中的旧视觉描述；保留你编辑的内容要求和讲稿。布局微调（树搜索）仍可在语言与内容的高级设置中选择。</p>
  </>}>
    <p className="ppt-intro">先选风格，再调整布局。所有预览随设置即时更新。</p>
    <fieldset className="ppt-theme-picker"><legend>PPT 风格</legend><div className="ppt-theme-grid">
      {slideThemes.map(theme => <label className="ppt-theme-choice" key={theme.id} data-selected={value.theme === theme.id}>
        <input type="radio" name={id} value={theme.id} aria-label={theme.label} checked={value.theme === theme.id} onChange={() => onChange({ ...value, theme: theme.id })} />
        <span className="ppt-theme-thumbnail" aria-hidden="true"><SlidePreview value={{ ...value, theme: theme.id }} page="cover" language={language} /></span>
        <span className="ppt-theme-name">{theme.label}<span className="ppt-theme-check" aria-hidden="true">{value.theme === theme.id ? '✓' : ''}</span></span>
        <span className="ppt-theme-description">{theme.description}</span>
      </label>)}
    </div></fieldset>
    <div className="ppt-workbench">
      <div className="ppt-preview-column"><div className="ppt-preview-toolbar">{pageButtons}<Button size="small" variant="text" onClick={() => setExpanded(true)}>放大预览与调整</Button></div>
        <SlidePreview value={value} page={page} language={language} />
        <p className="hint ppt-preview-note">排版示意 · 示例内容用于比较样式，实际课件按素材生成。</p>
      </div>
      <div className="ppt-adjustments"><div className="ppt-adjustment-head"><strong>调整当前方案</strong><button type="button" className="linkbtn" onClick={() => onChange({ ...defaultSlideDesign })}>恢复默认</button></div><DesignControls value={value} onChange={onChange} /></div>
    </div>
    <p className="ppt-current" role="status">将用于新任务：{slideDesignSummary(value)} · {slideDensities.find(item => item.id === value.density)!.label} · {value.font === 'sans' ? '无衬线字体' : '衬线字体'}</p>
    <Dialog header="PPT 风格与布局预览" dialogClassName="ppt-preview-dialog" placement="center" visible={expanded} onClose={() => setExpanded(false)} width="min(1160px, calc(100vw - 32px))" footer={<Button theme="primary" onClick={() => setExpanded(false)}>完成调整</Button>} destroyOnClose>
      <div className="ppt-dialog"><p className="ppt-intro">调整会同步到新任务。这里展示排版示意，生成后的课件请逐页审核。</p><div className="ppt-workbench"><div className="ppt-preview-column"><div className="ppt-preview-toolbar">{pageButtons}</div><SlidePreview value={value} page={page} language={language} /></div><DesignControls value={value} onChange={onChange} includeTheme /></div></div>
    </Dialog>
  </Section>;
}
