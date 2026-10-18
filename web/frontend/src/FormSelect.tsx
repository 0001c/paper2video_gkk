import { Children, isValidElement, type ReactNode } from 'react';
import { Select } from 'tdesign-react';

// 保留声明式选项，所有选择器共用 TDesign 的浮层、键盘交互与视觉样式。
export function FormSelect({ id, value, disabled, children, onChange, className, 'aria-label': label }: {
  id?: string; value: string | number; disabled?: boolean; children: ReactNode;
  onChange: (value: string) => void; className?: string; 'aria-label'?: string;
}) {
  const options = Children.toArray(children).flatMap(child => {
    if (!isValidElement<{ value: string | number; children: ReactNode; disabled?: boolean }>(child)) return [];
    return [{ value: String(child.props.value), label: Children.toArray(child.props.children).join(''), disabled: child.props.disabled }];
  });
  return <div className={`form-select ${className ?? ''}`} ref={element => {
    const input = element?.querySelector('input');
    if (input && id) input.id = id;
    if (input && label) input.setAttribute('aria-label', label);
  }}><Select value={String(value)} disabled={disabled} options={options}
    popupProps={{ overlayStyle: { maxWidth: 'calc(100vw - 32px)' } }}
    onChange={next => onChange(String(next))} /></div>;
}
