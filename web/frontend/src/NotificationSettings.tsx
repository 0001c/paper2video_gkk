import { useEffect, useRef, useState } from 'react';
import { Button } from 'tdesign-react';
import { api, errorMessage, runExclusive } from './flow';
import { Section } from './Section';

type Settings = { enabled: boolean; configured: boolean; server: string; completed: boolean; failed: boolean; review: boolean; error?: string };
export function NotificationSettings() {
  const [settings, setSettings] = useState<Settings>();
  const [url, setUrl] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [message, setMessage] = useState('');
  const [dirty, setDirty] = useState(false);
  const [revision, setRevision] = useState(0);
  const lock = useRef(false);
  useEffect(() => { let active = true;
    void api.get('/api/notifications/bark').then(value => { if (active) setSettings(value); })
      .catch(error => { if (active) setError(errorMessage(error)); });
    return () => { active = false; };
  }, [revision]);
  const save = () => runExclusive(lock, setBusy, async () => {
    if (!settings) return;
    setError(''); setMessage('');
    const { data } = await api.put('/api/notifications/bark', { enabled: settings.enabled, completed: settings.completed,
      failed: settings.failed, review: settings.review, ...(url.trim() ? { push_url: url.trim() } : {}) });
    setSettings(data); setUrl(''); setDirty(false); setMessage('通知配置已保存');
  }, error => setError(errorMessage(error)));
  const test = () => runExclusive(lock, setBusy, async () => {
    setError(''); setMessage('');
    const { data } = await api.post('/api/notifications/bark/test'); setMessage(data.message);
  }, error => setError(errorMessage(error)));
  const change = (field: 'enabled' | 'completed' | 'failed' | 'review', value: boolean) => {
    setSettings(previous => previous && { ...previous, [field]: value }); setDirty(true); setMessage('');
  };
  return <Section title="Bark 任务通知" compact defaultOpen={false} summary={dirty ? '有未保存修改' : settings?.enabled ? '已开启 · 后台推送' : '未开启'}
    help="适用于本机所有新运行的任务。发送任务编号和状态，不发送课文、讲稿、模型密钥或原始错误。推送地址按当前 Windows 账户加密保存，页面不回显设备 Key。中间格式重试和自动续跑不重复推送。">
    {error && <p className="notice err" role="alert">{error}{!settings && <Button variant="outline" onClick={() => { setError(''); setRevision(revision + 1); }}>重新读取配置</Button>}</p>}
    {message && <p className="notice ok" role="status">{message}</p>}
    {!settings ? <p className="hint">{error ? '通知配置暂未载入，请重新读取。' : '正在读取通知配置…'}</p> : <fieldset className="draft-fields" disabled={busy}>
      {settings.error && <p className="notice warn">{settings.error}</p>}
      <label className="checkrow"><input type="checkbox" checked={settings.enabled} onChange={event => change('enabled', event.target.checked)} />开启 Bark 通知</label>
      <div className="field"><label htmlFor="bark-url">Bark 推送地址</label>
        <input id="bark-url" type="password" autoComplete="off" value={url} onChange={event => { setUrl(event.target.value); setDirty(true); setMessage(''); }}
          placeholder={settings.configured ? '已保存，留空保留原地址' : 'https://api.day.app/设备Key'} />
        <p className="hint">{settings.configured ? `已配置服务：${settings.server}` : '填写 Bark App 中的完整推送地址，也支持自建服务。'}</p>
      </div>
      <div className="grid grid-3">{([['completed', '任务完成'], ['failed', '最终失败 / 评价异常'], ['review', '等待审核']] as const).map(([field, title]) =>
        <label className="checkrow" key={field}><input type="checkbox" checked={settings[field]} onChange={event => change(field, event.target.checked)} />{title}</label>)}</div>
      <p className="hint">评估输出格式错误每页最多自动重试两次；成功页直接复用。推送失败不会影响任务产物。</p>
      <div className="actions"><Button theme="primary" loading={busy} onClick={save}>保存通知配置</Button>
        <Button variant="outline" disabled={!settings.configured || dirty} loading={busy} onClick={test}>发送测试通知</Button></div>
    </fieldset>}
  </Section>;
}
