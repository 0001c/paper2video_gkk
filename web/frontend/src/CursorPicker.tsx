import { FormSelect } from './FormSelect';
import { useId } from 'react';
import { Help } from './Section';

export type CursorOption = { value: string; label: string; description: string; image_url: string | null };

export function CursorPicker({ options, style, scale, onStyle, onScale, disabled = false }: {
  options: CursorOption[]; style: string; scale: number; onStyle: (value: string) => void;
  onScale: (value: number) => void; disabled?: boolean;
}) {
  const id = useId();
  return <fieldset className="cursor-picker" disabled={disabled} aria-label="视频光标样式">
    <legend><span className="field-title">视频光标样式<Help title="视频光标样式">预览同时展示浅色和深色背景；箭头尖端、手指指尖或圆心对应讲解目标。只改变最终视觉标记，可复用未变化的讲稿与配音。</Help></span></legend>
    <div className="cursor-choices">
      {options.map((item) => <label key={item.value} className="cursor-choice" data-selected={style === item.value}>
        <input type="radio" name={id} value={item.value} checked={style === item.value} onChange={() => onStyle(item.value)} />
        <span className="cursor-preview" aria-hidden="true">{['light', 'dark'].map((background) => <span key={background} className={background}>
          {item.image_url ? <img src={item.image_url} alt="" style={{ width: 32 * scale, height: 32 * scale }} /> : <span className="cursor-none">∅</span>}
        </span>)}</span>
        <span className="cursor-choice-title">{item.label}<span>{style === item.value ? '已选' : ''}</span></span>
        <span className="cursor-description">{item.description}</span>
      </label>)}
    </div>
    <div className="field cursor-size"><div className="field-title"><label htmlFor={id + '-size'}>光标大小</label><Help title="光标大小">随成片画幅等比例缩放；关闭光标时无需设置大小。</Help></div><FormSelect id={id + '-size'} value={scale} disabled={style === 'none'} onChange={choice => onScale(Number(choice))}>
      <option value={0.75}>小 · 75%</option><option value={1}>标准 · 100%</option><option value={1.5}>大 · 150%</option>
    </FormSelect></div>
  </fieldset>;
}
