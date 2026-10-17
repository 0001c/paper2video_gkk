import { useMemo, type CSSProperties } from 'react';
import { groupJobHistory, jobDescendants, type JobOrigin } from './jobLineage';

export type HistoryJob = JobOrigin & {
  status: string; phase: string; source_name?: string; elapsed_min?: number;
  config?: { slide_lang?: string; script_lang?: string; stages?: string[]; reuse_script?: boolean; voice?: { kind: string; voice?: string } };
};

export function JobHistory({ jobs, selectedId, rerunning, onSelect, onRerun, dotOf }: {
  jobs: HistoryJob[]; selectedId: string | null; rerunning: boolean;
  onSelect: (id: string) => void; onRerun: (id: string) => void; dotOf: (status: string) => string;
}) {
  const families = useMemo(() => groupJobHistory(jobs), [jobs]);
  const row = (job: HistoryJob, derived = false) => <div className={`jobrow${job.id === selectedId ? ' active' : ''}`}>
    <span className={`dot ${dotOf(job.status)}`} />
    <div className="job-identity">
      <span className="job-origin-label">{derived ? '衍生任务' : job.resumed_from ? '来源记录不可用' : '初始任务'}</span>
      <span className="jfile">{job.source_name || '旧任务未记录文件名'}</span>
      <span className="jid">{job.id}</span>
      {job.resumed_from && <span className="job-parent-ref">来源：<button type="button" className="job-parent-link" onClick={() => onSelect(job.resumed_from!)} disabled={!jobs.some(item => item.id === job.resumed_from)}>{job.resumed_from}</button></span>}
    </div>
    <div className="job-context">
      <span className="jphase">{job.phase}{job.elapsed_min ? ` · ${job.elapsed_min} min` : ''}</span>
      <span className="jcfg">{job.config?.slide_lang}/{job.config?.script_lang}
        {job.config?.stages ? ` · stage ${job.config.stages.join('')}` : ''}
        {job.config?.reuse_script ? ' · 复用讲稿' : ''}
        {job.config?.voice && job.config.voice.kind !== 'default' ? ` · 音色 ${job.config.voice.voice || (job.config.voice.kind === 'design' ? '自定义' : '上传')}` : ''}
      </span>
    </div>
    <div className="jactions">
      <button type="button" className="linkbtn" onClick={() => onSelect(job.id)}>查看</button>
      {['done', 'failed', 'canceled'].includes(job.status) && <button type="button" className="linkbtn" disabled={rerunning} onClick={() => onRerun(job.id)}>{job.status === 'done' ? '重新合成视频' : '继续生成视频'}</button>}
    </div>
  </div>;
  return <div className="jobtable">{families.map(root => {
    const descendants = jobDescendants(root);
    return <section className="job-family" key={root.job.id} aria-label={`任务组 ${root.job.id}`}>
      {row(root.job)}
      {root.unavailableParent && <p className="job-origin-note">来源关系未能归入当前记录，请按来源编号核对。</p>}
      {descendants.length > 0 && <details className="job-descendants" open={descendants.some(({ node }) => node.job.id === selectedId)}>
        <summary>重新生成与续跑 · {descendants.length} 个衍生任务</summary>
        <ol className="job-children" aria-label="按生成顺序排列的衍生任务">{descendants.map(({ node, depth }) => <li className="job-child" key={node.job.id} style={{ '--job-depth': Math.min(depth, 3) } as CSSProperties}>
          <span className="job-depth-label">第 {depth} 层</span>{row(node.job, true)}
        </li>)}</ol>
      </details>}
    </section>;
  })}</div>;
}
