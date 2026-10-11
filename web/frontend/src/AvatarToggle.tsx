import { useState } from 'react';
import { Button, Dialog } from 'tdesign-react';
import { replaceStage, type ModelConfig } from './modelConfig';

export function localAvatar(value: ModelConfig): ModelConfig {
  return { ...replaceStage(value, 'avatar', { mode: 'local', provider: 'musetalk', model: 'musetalk', parameters: {} }),
    avatar: { enabled: true, mode: 'local', provider: 'musetalk', model: 'musetalk', parameters: {} } };
}

export function AvatarCostDialog({ value, onConfirm, onCancel }: { value: ModelConfig; onConfirm: (value: ModelConfig) => void; onCancel: () => void }) {
  return <Dialog visible header="开启数字人 · 先确认生成费用" onClose={onCancel} footer={<div className="actions">
    <Button variant="text" onClick={onCancel}>保持关闭 / 取消更改</Button>
    {value.avatar.mode === 'api' && <Button theme="warning" variant="outline" onClick={() => onConfirm(value)}>了解费用，使用 API</Button>}
    <Button theme="primary" onClick={() => onConfirm(localAvatar(value))}>使用本地 MuseTalk</Button>
  </div>}>
    <div className="notice warn"><strong>API 按生成视频时长累计计费，重生成会增加费用。</strong></div>
    <p>已有费用示例：264.37 秒、19 次检测及生成请求，约 <strong>¥132.19</strong>。实际费用取决于模型、时长和分辨率。</p>
    <p><strong>推荐本地模型推理。</strong>本地 MuseTalk 不产生数字人 API 费用，需要已安装模型与可用显存。默认关闭数字人也能生成完整讲解视频。</p>
  </Dialog>;
}

export function AvatarToggle({ value, onChange, disabled = false }: { value: ModelConfig; onChange: (value: ModelConfig) => void; disabled?: boolean }) {
  const [pending, setPending] = useState<ModelConfig>();
  return <>
    <label className="checkline material-toggle"><input type="checkbox" checked={value.avatar.enabled} disabled={disabled}
      onChange={(event) => event.target.checked ? setPending({ ...value, avatar: { ...value.avatar, enabled: true } })
        : onChange({ ...value, avatar: { ...value.avatar, enabled: false } })} />启用数字人</label>
    {pending && <AvatarCostDialog value={pending} onCancel={() => setPending(undefined)} onConfirm={(next) => { onChange(next); setPending(undefined); }} />}
  </>;
}
