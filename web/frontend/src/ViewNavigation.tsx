import { Children, type CSSProperties, type ReactNode, type Ref } from 'react';

const views = [
  { id: 'new', label: '新建任务' },
  { id: 'detail', label: '任务详情' },
  { id: 'history', label: '任务记录' },
] as const;
export type ViewId = typeof views[number]['id'];

export function ViewTabs({ view, hasJob, onChange, navRef }: {
  view: ViewId; hasJob: boolean; onChange: (view: ViewId) => void; navRef: Ref<HTMLElement>;
}) {
  return <nav className="view-tabs" aria-label="任务导航" ref={navRef}>
    <div className="view-tab-rail" style={{ '--view-index': views.findIndex(item => item.id === view) } as CSSProperties}>
      {views.map(item => <button key={item.id} type="button" aria-pressed={view === item.id}
        aria-controls={`view-${item.id}`} disabled={item.id === 'detail' && !hasJob} onClick={() => onChange(item.id)}>{item.label}</button>)}
      <span className="view-tab-indicator" aria-hidden="true" />
    </div>
  </nav>;
}

export function ViewPages({ view, children }: { view: ViewId; children: ReactNode }) {
  const activeIndex = views.findIndex(item => item.id === view);
  return <div className="view-pages">
    {Children.toArray(children).map((child, index) => <div key={views[index].id} id={`view-${views[index].id}`}
      className="view-page" data-active={index === activeIndex} aria-hidden={index !== activeIndex}
      style={{ '--page-offset': Math.sign(index - activeIndex) } as CSSProperties}
      ref={element => { if (element) element.inert = index !== activeIndex; }}>
      {child}
    </div>)}
  </div>;
}
