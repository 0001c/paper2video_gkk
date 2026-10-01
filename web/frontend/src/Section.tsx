import { useId, useState, type ReactNode } from 'react';
import { Popup } from 'tdesign-react';

export function Help({ title, children, important = false }: { title: string; children: ReactNode; important?: boolean }) {
  const id = useId();
  const [visible, setVisible] = useState(false);
  return <Popup trigger="click" placement="bottom-left" visible={visible} onVisibleChange={setVisible}
    destroyOnClose overlayClassName="editorial-help-popup" content={<div id={id} role="note">
      <strong>{important ? '重要提示 · ' : ''}{title}</strong><div className="editorial-help-copy">{children}</div>
    </div>}>
    <button type="button" className="editorial-help" data-important={important} aria-label={`查看${title}说明${important ? '（重要）' : ''}`}
      aria-expanded={visible} aria-controls={visible ? id : undefined} onKeyDown={(event) => { if (event.key === 'Escape') setVisible(false); }}>?</button>
  </Popup>;
}

export function Section({ title, number, summary, help, important, children, defaultOpen = true, compact = false, actions, label }: {
  title: string; number?: string; summary?: ReactNode; help?: ReactNode; important?: boolean;
  children: ReactNode; defaultOpen?: boolean; compact?: boolean; actions?: ReactNode; label?: string;
}) {
  const id = useId();
  const [open, setOpen] = useState(defaultOpen);
  const Heading = compact ? 'h3' : 'h2';
  return <section className={`section editorial-section${compact ? ' editorial-section-compact' : ''}`} data-open={open} aria-label={label ?? title}>
    <div className="editorial-section-head">
      <Heading className="section-title"><button className="section-toggle" type="button" aria-expanded={open} aria-controls={id}
        aria-label={`${open ? '折叠' : '展开'}${title}`} onClick={() => setOpen(!open)}>
        {number && <span className="section-num">{number}</span>}
        <span>{title}</span><span className="section-chevron" aria-hidden="true">⌄</span>
      </button></Heading>
      {help && <Help title={title} important={important}>{help}</Help>}
      {summary && <span className="section-summary">{summary}</span>}
      {actions && <div className="section-head-actions">{actions}</div>}
    </div>
    {/* 始终保留编辑控件；裁切容器随高度逐渐露出内容，关闭时阻止键盘进入。 */}
    <div className="section-reveal" id={id} aria-hidden={!open} ref={(element) => { if (element) element.inert = !open; }}>
      <div className="section-reveal-clip"><div className="section-content">{children}</div></div>
    </div>
  </section>;
}
