export const presentationSlides = [
  { id: 'opening', title: '从论文到讲解工坊', section: '论文' },
  { id: 'motivation', title: '一份论文，需要怎样的讲解？', section: '论文' },
  { id: 'outputs', title: 'Paper2Video 与 PaperTalker', section: '论文' },
  { id: 'pipeline', title: '四个 Builder 协同生成', section: '论文' },
  { id: 'alignment', title: '版面、讲解与指向如何对齐', section: '论文' },
  { id: 'evaluation', title: '结果与评价的边界', section: '论文' },
  { id: 'bridge', title: '从论文方法到教学工作流', section: '论文' },
  { id: 'demo', title: '一份真实课程的五步体验', section: '流程体验' },
  { id: 'teaching', title: '把教学意图写进生成过程', section: '工作台进阶' },
  { id: 'configuration', title: '按环节配置模型与资源', section: '工作台进阶' },
  { id: 'reliability', title: '评价、复核与失败恢复', section: '工作台进阶' },
  { id: 'closing', title: '已完成的工作与下一步验证', section: '工作台进阶' },
] as const;

export type PresentationSlide = typeof presentationSlides[number]['id'];

export function isPresentationSlide(value: string | null): value is PresentationSlide {
  return presentationSlides.some(slide => slide.id === value);
}

// 上下文只属于当前链接，不写入全局存储；普通工作台标签页不会继承汇报状态。
export function presentationContext(search: string): PresentationSlide | null {
  const query = new URLSearchParams(search);
  if (query.get('from') !== 'presentation') return null;
  const slide = query.get('returnSlide');
  return isPresentationSlide(slide) ? slide : 'demo';
}

export function presentationHref(slide: PresentationSlide) {
  return `/presentation/#${slide}`;
}

export function presentationEntry(path: '/' | '/demo/', slide: PresentationSlide) {
  return `${path}?from=presentation&returnSlide=${slide}`;
}

export function demoWorkbenchHref(search: string) {
  return presentationContext(search) ? presentationEntry('/', 'teaching') : '/';
}
