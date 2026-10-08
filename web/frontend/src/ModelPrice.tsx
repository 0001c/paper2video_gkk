import { useId, useState, type ReactNode } from 'react';
import { Popup } from 'tdesign-react';

export type Pricing = { groups: { range_name: string; prices: { type: string; price: string; price_name: string; price_unit: string; time_band: string }[] }[];
  currency: string | null; source: string; queried_at: string | null; region: string; reference_url: string };
export type CatalogModel = { id: string; name?: string; selectable: boolean; adapter_implemented: boolean; status: string; verification: string;
  offline_time: string | null; model_info?: { max_output_tokens?: number }; capabilities?: string[]; roles: string[];
  role_support: Record<string, { selectable: boolean; adapter_implemented: boolean; verification: string; recommended: boolean }>; pricing?: Pricing };

export function ModelPrice({ model, pricing, children, disabled = false, triggerElement }: { model: string; pricing?: Pricing; children: ReactNode; disabled?: boolean; triggerElement?: string }) {
  const id = useId();
  const [visible, setVisible] = useState(false);
  return <Popup trigger="hover" triggerElement={triggerElement} placement="left-top" delay={[180, 150]} destroyOnClose disabled={disabled}
    popperOptions={{ modifiers: [{ name: 'preventOverflow', options: { altAxis: true, padding: 16 } }] }}
    visible={!disabled && visible} onVisibleChange={setVisible} overlayClassName="model-price-popup"
    content={<div id={id} role="tooltip" className="model-price-detail">
      <strong>{model}</strong><p className="model-price-meta">{pricing?.currency === 'CNY' ? '人民币 CNY · 目录标价' : pricing?.currency ? `${pricing.currency} · 目录标价` : '目录未声明币种'} · {pricing?.region || '未查询连接'}</p>
      {pricing?.groups.length ? <div className="model-price-scroll"><table><thead><tr><th>计费项</th><th>单价</th><th>单位</th></tr></thead><tbody>
        {pricing.groups.map((group, index) => <PriceGroup key={index} group={group} currency={pricing.currency} />)}
      </tbody></table></div> : <p className="model-price-empty">暂无官方价格数据，不能据此视为免费。请刷新目录或查看官方价格页。</p>}
      <p className="model-price-meta">查询时间：{pricing?.queried_at ? new Date(pricing.queried_at).toLocaleString('zh-CN', { hour12: false }) : '尚未查询'}{pricing?.source === 'workspace_snapshot' ? ' · 历史快照' : ''}</p>
      <p className="model-price-meta">阶梯及特殊计费项按对应条件生效；优惠、免费额度与实际扣费以控制台账单为准。</p>
      <a href={pricing?.reference_url || 'https://help.aliyun.com/zh/model-studio/model-pricing'} target="_blank" rel="noreferrer">查看官方价格说明 ↗</a>
    </div>}>
    <span className="model-price-anchor" aria-describedby={visible && !disabled ? id : undefined}
      onFocusCapture={() => setVisible(true)} onBlurCapture={() => setVisible(false)}
      onKeyDown={(event) => { if (event.key === 'Escape') setVisible(false); }}>{children}</span>
  </Popup>;
}

function PriceGroup({ group, currency }: { group: Pricing['groups'][number]; currency: string | null }) {
  return <><tr className="model-price-tier"><th colSpan={3}>{group.range_name === 'Default' ? '标准计费' : group.range_name}</th></tr>
    {group.prices.map((row, index) => <tr key={index}><td>{row.price_name || row.type}{row.time_band && row.time_band !== 'standard' && <small> · {row.time_band}</small>}</td>
      <td>{currency === 'CNY' ? '¥' : ''}{row.price}</td><td>{row.price_unit || '单位未提供'}</td></tr>)}</>;
}
