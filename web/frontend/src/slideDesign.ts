export const slideThemes = [
  { id: 'academic', label: '学术蓝', description: '清晰严谨 · 研究与汇报', background: '#F7F9FC', ink: '#172D4B', accent: '#245CA6', panel: '#E7EEF8' },
  { id: 'classroom', label: '课堂绿', description: '平和专注 · 概念与教学', background: '#F5F8F2', ink: '#253E31', accent: '#35654C', panel: '#E3EDDF' },
  { id: 'warm', label: '暖纸', description: '温润舒展 · 人文与叙事', background: '#FCF6EB', ink: '#493B2D', accent: '#9B502D', panel: '#F1E4CE' },
  { id: 'midnight', label: '深色演示', description: '高对比度 · 演讲与展示', background: '#152238', ink: '#F0F5FD', accent: '#8BBEFF', panel: '#243A57' },
] as const;

export const slideLayouts = [
  { id: 'split', label: '图文并列', description: '文字与图示分栏，适合概念讲解', instruction: 'Use two aligned columns: concise explanation on the left, source-supported figure, formula or process on the right. Do not invent a figure when the source has none.' },
  { id: 'focus', label: '重点聚焦', description: '核心观点居中，适合公式与结论', instruction: 'Center one main idea, equation or source figure, with supporting explanation below it. Keep the visual center balanced and avoid competing focal points.' },
  { id: 'compare', label: '双栏对照', description: '左右等宽，适合比较与归纳', instruction: 'Use two equal-width aligned panels for source-supported comparisons or parallel concepts. When comparison is not appropriate, use the columns for a concept and its supporting evidence; never invent a comparison.' },
] as const;

export const slideDensities = [
  { id: 'airy', label: '疏朗 · 重点先行', instruction: 'Prefer 2 main points per content frame, generous whitespace, and large readable type.' },
  { id: 'standard', label: '均衡 · 图文兼顾', instruction: 'Prefer 3 main points per content frame, with balanced whitespace and readable type.' },
  { id: 'detailed', label: '紧凑 · 补充细节', instruction: 'Use up to 4 concise main points per content frame. Preserve readable type; split overflow rather than shrink text.' },
] as const;

export type SlideDesign = {
  theme: typeof slideThemes[number]['id'];
  layout: typeof slideLayouts[number]['id'];
  density: typeof slideDensities[number]['id'];
  font: 'sans' | 'serif';
  ratio: 'wide' | 'standard';
};
export const defaultSlideDesign: SlideDesign = { theme: 'academic', layout: 'split', density: 'standard', font: 'sans', ratio: 'wide' };

export function readSlideDesign(value: unknown): SlideDesign | undefined {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return undefined;
  const raw = value as Record<string, unknown>;
  const allowed = {
    theme: slideThemes.map(item => item.id), layout: slideLayouts.map(item => item.id),
    density: slideDensities.map(item => item.id), font: ['sans', 'serif'], ratio: ['wide', 'standard'],
  };
  if (Object.keys(raw).some(key => !Object.keys(allowed).includes(key))) return undefined;
  for (const [key, values] of Object.entries(allowed)) {
    if (raw[key] !== undefined && !(values as readonly unknown[]).includes(raw[key])) return undefined;
  }
  return { ...defaultSlideDesign, ...Object.fromEntries(Object.entries(raw).filter(([, item]) => item !== undefined)) } as SlideDesign;
}

export function slideDesignSummary(value: SlideDesign): string {
  return `${slideThemes.find(item => item.id === value.theme)!.label} · ${slideLayouts.find(item => item.id === value.layout)!.label} · ${value.ratio === 'wide' ? '16:9' : '4:3'}`;
}

// 只在提交时组合，原始提示词独立保存；反复改样式不会累积指令或覆盖用户编辑。
// 后端已有 slide_prompt 的任务快照、哈希与缓存失效流程，沿用同一条生成路径。
export function composeSlidePrompt(base: string, value: SlideDesign): string {
  const theme = slideThemes.find(item => item.id === value.theme)!;
  const layout = slideLayouts.find(item => item.id === value.layout)!;
  const density = slideDensities.find(item => item.id === value.density)!;
  return `${base.trim()}\n\n--- Presentation visual specification ---
Apply the following visual settings consistently to the complete LaTeX Beamer presentation. They take precedence over earlier visual styling instructions only; preserve all source facts, requested language, content structure and target slide count.
Theme: ${theme.label} (${theme.id}). Define xcolor HTML colors: background ${theme.background.slice(1)}, foreground ${theme.ink.slice(1)}, accent ${theme.accent.slice(1)}, panel ${theme.panel.slice(1)}. Apply these to normal text, frame titles, structure and block backgrounds. Use a restrained accent rule, plain backgrounds, aligned content and a simple page-number footer; remove navigation symbols and avoid heavy theme decorations.
Set the Beamer documentclass aspectratio=${value.ratio === 'wide' ? '169' : '43'}.
Typography: ${value.font === 'sans' ? 'use a clean sans-serif family (Beamer default sans text)' : 'use a readable serif family (Beamer font theme serif)'}. Preserve the existing CJK setup for Chinese text, use available fonts only, and keep headings clearly larger than body text.
Content layout preference: ${layout.instruction} Adapt cover and summary frames to their purpose; do not force every frame into the same layout.
Content density: ${density.instruction} Never remove required teaching coverage to satisfy density; shorten wording or rebalance content within the requested slide count.
Keep all elements inside safe margins. Use real source figures only. Output complete compilable LaTeX Beamer code with documentclass and frame environments as required above.`;
}
