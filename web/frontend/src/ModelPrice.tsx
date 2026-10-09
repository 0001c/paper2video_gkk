import { createContext, useCallback, useContext, useEffect, useId, useRef, useState, type ReactNode } from 'react';
import { Popup } from 'tdesign-react';

export type Pricing = { groups: { range_name: string; prices: { type: string; price: string; price_name: string; price_unit: string; time_band: string }[] }[];
  currency: string | null; source: string; queried_at: string | null; region: string; reference_url: string };
export type CatalogModel = { id: string; name?: string; selectable: boolean; adapter_implemented: boolean; status: string; verification: string;
  offline_time: string | null; model_info?: { max_output_tokens?: number }; capabilities?: string[]; roles: string[];
  role_support: Record<string, { selectable: boolean; adapter_implemented: boolean; verification: string; recommended: boolean }>; pricing?: Pricing };

type PriceTarget = { id: string; model: string; pricing?: Pricing; selector: string };
const PriceContext = createContext<{ enter: (target: PriceTarget, immediate?: boolean) => void; leave: (id: string) => void; dismiss: (id: string) => void } | null>(null);

// A single popup also prevents delayed hovers on disabled Select rows from stacking portals.
export function ModelPriceProvider({ children }: { children: ReactNode }) {
  const [target, setTarget] = useState<PriceTarget>();
  const current = useRef<PriceTarget>();
  const timer = useRef<ReturnType<typeof setTimeout>>();
  const cancel = useCallback(() => clearTimeout(timer.current), []);
  const close = useCallback(() => { cancel(); current.current = undefined; setTarget(undefined); }, [cancel]);
  const enter = useCallback((next: PriceTarget, immediate = false) => {
    cancel();
    const alreadyOpen = current.current?.id === next.id;
    current.current = next;
    if (immediate || alreadyOpen) setTarget(next);
    else timer.current = setTimeout(() => setTarget(next), 180);
  }, [cancel]);
  const leave = useCallback((id: string) => {
    if (current.current?.id !== id) return;
    cancel();
    timer.current = setTimeout(close, 150);
  }, [cancel, close]);
  const dismiss = useCallback((id: string) => { if (current.current?.id === id) close(); }, [close]);
  useEffect(() => {
    if (!target) return;
    const anchor = document.querySelector(target.selector);
    anchor?.setAttribute('aria-describedby', `${target.id}-price`);
    return () => { anchor?.removeAttribute('aria-describedby'); };
  }, [target]);
  useEffect(() => {
    const escape = (event: KeyboardEvent) => { if (event.key === 'Escape') close(); };
    const scroll = (event: Event) => { if (!(event.target instanceof Element) || !event.target.closest('.model-price-popup')) close(); };
    document.addEventListener('keydown', escape);
    document.addEventListener('scroll', scroll, true);
    window.addEventListener('blur', close);
    return () => { cancel(); document.removeEventListener('keydown', escape); document.removeEventListener('scroll', scroll, true); window.removeEventListener('blur', close); };
  }, [cancel, close]);
  return <PriceContext.Provider value={{ enter, leave, dismiss }}>{children}{target && <Popup trigger="click" triggerElement={target.selector} placement="left-top" destroyOnClose visible
    onVisibleChange={(visible) => { if (!visible) close(); }} overlayClassName="model-price-popup"
    popperOptions={{ modifiers: [{ name: 'preventOverflow', options: { altAxis: true, padding: 16 } }] }}
    content={<div onPointerEnter={cancel} onPointerLeave={() => leave(target.id)} onFocusCapture={cancel} onBlurCapture={(event) => { if (!event.currentTarget.contains(event.relatedTarget)) leave(target.id); }}>
      <PriceDetail model={target.model} pricing={target.pricing} id={`${target.id}-price`} />
    </div>} />}</PriceContext.Provider>;
}

export function ModelPrice({ model, pricing, children, disabled = false, triggerElement }: { model: string; pricing?: Pricing; children: ReactNode; disabled?: boolean; triggerElement?: string }) {
  const id = useId();
  const controls = useContext(PriceContext);
  const selector = triggerElement || `[data-model-price-anchor="${id}"]`;
  useEffect(() => {
    if (!controls || disabled) return;
    const anchor = document.querySelector(selector);
    if (!anchor) return;
    const next = { id, model, pricing, selector };
    const enter = () => controls.enter(next);
    const focus = () => controls.enter(next, true);
    const leave = () => controls.leave(id);
    anchor.addEventListener('pointerenter', enter);
    anchor.addEventListener('pointerleave', leave);
    anchor.addEventListener('focusin', focus);
    anchor.addEventListener('focusout', leave);
    return () => { anchor.removeEventListener('pointerenter', enter); anchor.removeEventListener('pointerleave', leave); anchor.removeEventListener('focusin', focus); anchor.removeEventListener('focusout', leave); controls.dismiss(id); };
  }, [controls?.enter, controls?.leave, controls?.dismiss, disabled, id, model, pricing, selector]);
  return <span className="model-price-anchor" data-model-price-anchor={id}>{children}</span>;
}

function PriceDetail({ model, pricing, id }: { model: string; pricing?: Pricing; id: string }) {
  return <div id={id} role="tooltip" className="model-price-detail">
      <strong>{model}</strong><p className="model-price-meta">{pricing?.currency === 'CNY' ? '人民币 CNY · 目录标价' : pricing?.currency ? `${pricing.currency} · 目录标价` : '目录未声明币种'} · {pricing?.region || '未查询连接'}</p>
      {pricing?.groups.length ? <div className="model-price-scroll"><table><thead><tr><th>计费项</th><th>单价</th><th>单位</th></tr></thead><tbody>
        {pricing.groups.map((group, index) => <PriceGroup key={index} group={group} currency={pricing.currency} />)}
      </tbody></table></div> : <p className="model-price-empty">暂无官方价格数据，不能据此视为免费。请刷新目录或查看官方价格页。</p>}
      <p className="model-price-meta">查询时间：{pricing?.queried_at ? new Date(pricing.queried_at).toLocaleString('zh-CN', { hour12: false }) : '尚未查询'}{pricing?.source === 'workspace_snapshot' ? ' · 历史快照' : ''}</p>
      <p className="model-price-meta">阶梯及特殊计费项按对应条件生效；优惠、免费额度与实际扣费以控制台账单为准。</p>
      <a href={pricing?.reference_url || 'https://help.aliyun.com/zh/model-studio/model-pricing'} target="_blank" rel="noreferrer">查看官方价格说明 ↗</a>
    </div>;
}

function PriceGroup({ group, currency }: { group: Pricing['groups'][number]; currency: string | null }) {
  return <><tr className="model-price-tier"><th colSpan={3}>{group.range_name === 'Default' ? '标准计费' : group.range_name}</th></tr>
    {group.prices.map((row, index) => <tr key={index}><td>{row.price_name || row.type}{row.time_band && row.time_band !== 'standard' && <small> · {row.time_band}</small>}</td>
      <td>{currency === 'CNY' ? '¥' : ''}{row.price}</td><td>{row.price_unit || '单位未提供'}</td></tr>)}</>;
}
