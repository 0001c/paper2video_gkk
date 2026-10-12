import { useEffect, useState } from 'react';
import { api, errorMessage } from './flow';
import { Section } from './Section';

type Version = { id: string; created_at: string; job_id: string; reason: string; size_bytes: number; video_sha256: string };
export function VideoVersions({ jobId, refreshKey }: { jobId: string; refreshKey: string }) {
  const [items, setItems] = useState<Version[]>([]);
  const [error, setError] = useState('');
  const [reload, setReload] = useState(0);
  useEffect(() => {
    let active = true;
    setError(''); setItems([]);
    void api.get(`/api/jobs/${jobId}/video-versions`).then(data => { if (active) setItems(data.items); })
      .catch(error => { if (active) setError(errorMessage(error)); });
    return () => { active = false; };
  }, [jobId, refreshKey, reload]);
  return <Section title="成片版本备份" compact defaultOpen={false} summary={`${items.length} 份备份`}
    help="合片完成和每次修复前自动保存视频、讲稿、配置与评价快照。修复任务使用独立产物目录；备份不会自动清理，下载历史版本不会替换当前成片。">
    {error ? <p className="notice warn" role="alert">{error}<button type="button" className="linkbtn" onClick={() => setReload(reload + 1)}>重新加载备份</button></p>
      : !items.length ? <p className="hint">完整成片产出或再次修复时会保存备份。</p>
      : <div className="video-version-list">{items.map((item, index) => <div className="video-version" key={item.id}>
        <div><strong>{index === 0 ? '最新备份' : `历史备份 ${items.length - index}`} · {item.reason}</strong>
          <p className="hint">{new Date(item.created_at).toLocaleString('zh-CN', { hour12: false })} · {(item.size_bytes / 2 ** 20).toFixed(1)} MB · {item.job_id}</p></div>
        <div className="actions"><a href={`/api/jobs/${jobId}/video-versions/${item.id}/video`} target="_blank" rel="noreferrer">下载成片</a>
          <a href={`/api/jobs/${jobId}/video-versions/${item.id}/report`} target="_blank" rel="noreferrer">评价与配置</a>
          <a href={`/api/jobs/${jobId}/video-versions/${item.id}/script`} target="_blank" rel="noreferrer">讲稿</a></div>
      </div>)}</div>}
  </Section>;
}
