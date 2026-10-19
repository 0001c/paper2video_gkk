import { useEffect, useState } from 'react';
import { presentationContext, presentationHref } from './presentationNavigation';
import './presentation-return.css';

export function PresentationReturn() {
  const [slide, setSlide] = useState(() => presentationContext(window.location.search));
  useEffect(() => {
    const update = () => setSlide(presentationContext(window.location.search));
    window.addEventListener('popstate', update);
    return () => window.removeEventListener('popstate', update);
  }, []);
  if (!slide) return null;
  return <div className="presentation-return-space">
    <a className="presentation-return" href={presentationHref(slide)} aria-label="返回汇报，继续当前章节">
      <span aria-hidden="true">↖</span> 返回汇报
    </a>
  </div>;
}
