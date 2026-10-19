import { useRef, useState } from 'react';
import { AccessControls } from '../AccessGate';
import { ThemeToggle } from '../ThemeToggle';
import sample from './sample.json';
import { demoWorkbenchHref, presentationContext, presentationHref } from '../presentationNavigation';

const stages = [
  ['素材准备', '载入教材，预览正文'], ['教学编排', '明确目标与章节安排'],
  ['课件与讲稿', '逐页预览，体验审核'], ['配音与合片', '试听声音，串联内容'],
  ['成片与评价', '播放视频，查看证据'],
];
const file = (path: string) => `/api/jobs/${sample.job_id}/files/${path.split('/').map(encodeURIComponent).join('/')}`;
const clock = (seconds: number) => `${Math.floor(seconds / 60).toString().padStart(2, '0')}:${Math.floor(seconds % 60).toString().padStart(2, '0')}`;
const pageAt = (seconds: number) => {
  const next = sample.pages.findIndex(p => p.start_seconds > seconds);
  return next === -1 ? sample.pages.length - 1 : Math.max(0, next - 1);
};

export default function Demo() {
  const fromPresentation = presentationContext(window.location.search);
  const workbenchHref = demoWorkbenchHref(window.location.search);
  const [step, setStep] = useState(0);
  const [loaded, setLoaded] = useState(false);
  const [source, setSource] = useState(0);
  const [page, setPage] = useState(0);
  const [videoPage, setVideoPage] = useState(0);
  const [drafts, setDrafts] = useState(() => sample.pages.map(p => p.script.join('\n')));
  const [accepted, setAccepted] = useState(false);
  const [assembled, setAssembled] = useState(false);
  const [notice, setNotice] = useState('');
  const [mediaError, setMediaError] = useState('');
  const [answers, setAnswers] = useState<Record<number, string>>({});
  const video = useRef<HTMLVideoElement>(null);
  const pendingSeek = useRef<number | null>(null);
  const panel = useRef<HTMLElement>(null);
  const current = sample.pages[page];
  const edited = drafts.some((draft, i) => draft !== sample.pages[i].script.join('\n'));

  function go(next: number) {
    setStep(next); setNotice(''); setMediaError('');
    pendingSeek.current = null;
    if (next === 4) setVideoPage(0);
    requestAnimationFrame(() => {
      panel.current?.focus({ preventScroll: true });
      panel.current?.scrollIntoView({ block: 'start' });
    });
  }
  function reset() {
    setLoaded(false); setSource(0); setPage(0); setAccepted(false); setAssembled(false);
    setDrafts(sample.pages.map(p => p.script.join('\n'))); setAnswers({}); go(0);
  }
  function seek(seconds: number) {
    if (!video.current) return;
    setVideoPage(pageAt(seconds));
    if (video.current.readyState < 1) pendingSeek.current = seconds;
    else video.current.currentTime = seconds;
    setNotice(`已定位到 ${clock(seconds)}，点击播放器播放即可观看。`);
  }

  return <div className="demo-shell">
    <header className="demo-header">
      <a className="brand" href={workbenchHref}>讲解工坊<span className="brand-caption">PAPER2VIDEO</span></a>
      <div className="demo-header-actions">{fromPresentation && <a href={presentationHref(fromPresentation)}>返回汇报 ↖</a>}<a href={workbenchHref}>进入工作台 ↗</a><AccessControls /><ThemeToggle /></div>
    </header>
    <main>
      <section className="demo-hero" aria-labelledby="demo-title">
        <div><p className="eyebrow">THE MAKING OF A LESSON / 流程体验</p>
          <h1 id="demo-title">从一份教材，<br />到一堂可观看的课。</h1>
          <p className="demo-lead">跟随一份真实产物，走一遍素材提取、教学编排、课件审核与视频生成。每一步，都有内容可看。</p>
          <div className="demo-hero-actions"><button className="demo-primary" onClick={() => { setLoaded(true); go(0); setNotice('示例素材已载入，可以预览正文并继续教学编排。'); }}>载入示例，开始体验 <span aria-hidden="true">→</span></button>
            <button className="demo-text-button" onClick={() => { setLoaded(true); go(4); }}>先看成片 ↗</button></div>
          <p className="demo-mode">演示模式 · 使用已有产物 · 无需配置模型或密钥</p>
        </div>
        <figure className="demo-cover"><img src={file(sample.pages[0].slide)} alt="本次示例课程的真实封面：大语言模型的构建过程与扩展法则" onError={() => setMediaError('示例课件暂时无法读取，请确认原任务产物仍保存在本机。')} />
          <figcaption><span>本次体验课程</span><strong>{sample.title}</strong><span>{sample.pages.length} 页课件 <i> / </i> {clock(sample.duration_seconds)} 成片 <i> / </i> 中文讲解</span></figcaption>
        </figure>
      </section>

      <div className="demo-flow-heading"><p className="eyebrow">FOLLOW THE PROCESS</p><span>按步骤体验，也可在载入后自由切换</span><button className="demo-text-button" onClick={reset}>重新体验 ↺</button></div>
      <nav className="demo-steps" aria-label="演示流程">
        {stages.map(([label, description], i) => <button key={label} disabled={!loaded && i !== 0} aria-current={step === i ? 'step' : undefined}
          onClick={() => go(i)}><span className="demo-step-number">{String(i + 1).padStart(2, '0')}</span><span><strong>{label}</strong><small>{description}</small></span></button>)}
      </nav>

      <section className="demo-panel" ref={panel} tabIndex={-1} aria-labelledby="demo-panel-title">
        <div className="demo-panel-heading"><div><p className="eyebrow">STEP {String(step + 1).padStart(2, '0')} / 05</p><h2 id="demo-panel-title">{stages[step][0]}</h2></div><span className="demo-tag">{step === 4 ? '真实成片与评价' : '已有任务产物体验'}</span></div>
        <p className="demo-safety">这里的操作仅用于体验；页面内的讲稿修改不会保存到原任务，也不会重新生成音频或视频。</p>
        <div className="demo-feedback" aria-live="polite">{notice && <p className="notice ok">{notice}</p>}</div>
        {mediaError && <p className="notice warn" role="alert">{mediaError} <button className="demo-text-button" onClick={() => window.location.reload()}>重新载入</button></p>}

        {step === 0 && <div className="demo-content-grid">
          <div><div className="demo-file"><span className="demo-file-icon" aria-hidden="true">PDF</span><div><strong>{sample.source_name}</strong><p>教材节选 · 历史提取与教学范围快照</p></div><span className="demo-tag">{loaded ? '已载入' : '示例素材'}</span></div>
            <h3>先让内容变得可读</h3><p className="demo-body-copy">正式流程从上传 PDF、Word 或 LaTeX 开始，提取正文后检查内容，再确定要讲解的章节。这里已准备好本次任务使用的素材。</p>
            <div className="demo-facts"><div><span>素材语言</span><strong>中文</strong></div><div><span>教学范围</span><strong>{sample.plan.sections.length} 个章节</strong></div><div><span>课件页数</span><strong>{sample.pages.length} 页</strong></div></div>
            <p className="demo-caption">正文与章节名称保留该历史任务使用时的快照，可能包含旧提取器的页眉与章节归属误差。</p>
          </div>
          <div><label className="demo-label" htmlFor="demo-source">查看所选章节原文</label><select id="demo-source" value={source} onChange={e => setSource(Number(e.target.value))}>{sample.plan.sections.map((s, i) => <option key={s.id} value={i}>{s.title}</option>)}</select>
            <div className="demo-source-text">{sample.plan.sections[source].source}</div></div>
        </div>}

        {step === 1 && <div className="demo-content-grid">
          <div><h3>先安排怎么教，再决定怎么讲</h3><p className="demo-body-copy">教学对象、目标与难点共同约束内容深度；章节安排连接后续课件和逐页讲稿。</p>
            <dl className="demo-plan"><dt>教学对象</dt><dd>{sample.plan.audience}</dd><dt>教学目标</dt><dd>{sample.plan.objectives}</dd><dt>重点与难点</dt><dd>{sample.plan.focus}</dd></dl></div>
          <div><h3>本次教学安排</h3><div className="demo-section-list">{sample.plan.sections.map((s, i) => <div key={s.id}><span className="demo-step-number">{String(i + 1).padStart(2, '0')}</span><div><strong>{s.title}</strong><p>第 {s.first_page}–{s.last_page} 页课件 · 参考 {s.minutes.toFixed(1)} 分钟</p></div></div>)}</div>
            <div className="demo-note"><strong>参考编排 {sample.plan.planned_minutes} 分钟 → 实际成片 {clock(sample.duration_seconds)}</strong><p>时长用于引导讲解节奏，实际结果取决于讲稿长度和配音语速。</p></div>
            <details><summary>查看讲解深度要求</summary><p>{sample.plan.depth}</p></details>
          </div></div>}

        {step === 2 && <><div className="demo-review-layout"><aside className="demo-page-list" aria-label="逐页课件">{sample.pages.map((p, i) => <button key={p.page} aria-current={page === i ? 'true' : undefined} onClick={() => { setPage(i); setMediaError(''); }}><span>{String(p.page).padStart(2, '0')}</span><span>{p.title}</span></button>)}</aside>
          <div className="demo-slide-preview"><a href={file(current.slide)} target="_blank" rel="noreferrer"><img key={current.slide} src={file(current.slide)} alt={`第 ${current.page} 页：${current.title}`} onError={() => setMediaError('当前课件图片读取失败，请确认原任务产物可用。')} /></a><div className="demo-page-controls"><button disabled={page === 0} onClick={() => setPage(page - 1)}>← 上一页</button><span>第 {page + 1} / {sample.pages.length} 页</span><button disabled={page === sample.pages.length - 1} onClick={() => setPage(page + 1)}>下一页 →</button></div><p className="demo-caption">点击课件可查看原图。</p></div>
          <div className="demo-script"><label htmlFor="demo-script" className="demo-label">第 {current.page} 页讲稿 <span>可在此试改</span></label><textarea id="demo-script" value={drafts[page]} onChange={e => { setDrafts(drafts.map((draft, i) => i === page ? e.target.value : draft)); setAccepted(false); }} />
            <button className="demo-text-button" onClick={() => { setDrafts(drafts.map((draft, i) => i === page ? current.script.join('\n') : draft)); setAccepted(false); setNotice('本页已恢复为原任务讲稿。'); }}>恢复本页示例讲稿</button>
            <details><summary>查看原始光标目标</summary><ul className="demo-cursor-list">{current.cursors.filter(c => c !== 'no').map((c, i) => <li key={i}>{c}</li>)}</ul><p className="demo-caption">光标根据这些原始目标指向课件中的对应内容。</p></details></div>
        </div><label className="demo-confirm"><input type="checkbox" checked={accepted} onChange={e => setAccepted(e.target.checked)} />我已体验逐页核对，继续查看配音与合片</label>{edited && <p className="demo-caption">讲稿试改仅保留在当前页面；后续播放的是原任务的声音和视频。</p>}</>}

        {step === 3 && <div className="demo-content-grid"><div><h3>让课件有声音，让讲解有指向</h3><p className="demo-body-copy">逐页讲稿转为配音，光标跟随讲解，字幕进入画面，最后合成为完整课程。本例使用配音、字幕与光标，没有数字人。</p>
          <ol className="demo-production">{['课件与逐页讲稿', `${sample.pages.length} 页真实配音`, '讲解光标与中文字幕', `${clock(sample.duration_seconds)} 完整课程`].map((label, i) => <li key={label}><span>{i + 1}</span><div><strong>{label}</strong><small>原任务已完成</small></div><span aria-label="已完成">✓</span></li>)}</ol>
          {assembled && <p className="notice ok" role="status">体验步骤已完成，已准备好原任务成片。</p>}</div>
          <div><label className="demo-label" htmlFor="demo-audio-page">选择一页，试听真实配音</label><select id="demo-audio-page" value={page} onChange={e => { setPage(Number(e.target.value)); setMediaError(''); }}>{sample.pages.map((p, i) => <option key={p.page} value={i}>{p.page}. {p.title}</option>)}</select>
            <audio key={current.audio} controls preload="none" src={file(current.audio)} onError={() => setMediaError('配音文件无法读取，请确认原任务的音频仍在。')}>浏览器不支持音频播放。</audio><p className="demo-caption">本页实际配音 {clock(current.seconds)} · 原任务音频</p>
            <div className="demo-audio-script">{current.script.join('\n')}</div>
            <button className="demo-primary" onClick={() => { setAssembled(true); setNotice('配音、光标、字幕已在原任务中完成。现在可继续查看真实成片。'); }}>体验合片完成 <span aria-hidden="true">✓</span></button>
            <p className="demo-caption">此按钮演示完成确认，不触发生成任务。</p></div></div>}

        {step === 4 && <><div className="demo-result-grid"><div><video ref={video} controls playsInline preload="metadata" poster={file(sample.pages[0].slide)} src={file('3_merage.mp4')} onTimeUpdate={e => {
            setVideoPage(pageAt(e.currentTarget.currentTime));
          }} onLoadedMetadata={() => {
            if (pendingSeek.current !== null && video.current) { video.current.currentTime = pendingSeek.current; pendingSeek.current = null; }
          }} onError={() => setMediaError('成片暂时无法播放，请检查原任务产物，或使用下方下载链接。')}>浏览器不支持视频播放。</video>
          <div className="demo-chapters" aria-label="视频章节">{sample.chapters.map(c => <button key={c.title} onClick={() => seek(c.start_seconds)}><span>{clock(c.start_seconds)}</span>{c.title}</button>)}</div>
          <label className="demo-label" htmlFor="demo-video-page">按课件页定位视频</label><select id="demo-video-page" value={videoPage} onChange={e => { const i = Number(e.target.value); seek(sample.pages[i].start_seconds); }}>{sample.pages.map((p, i) => <option key={p.page} value={i}>{clock(p.start_seconds)} · {p.page}. {p.title}</option>)}</select>
          <div className="demo-downloads"><a href={file('3_merage.mp4')} download>下载成片 MP4 ↓</a><a href={file('slides_rendered.pdf')} download>下载课件 PDF ↓</a><a href={file('subtitle_w_cursor.txt')} download>下载原始讲稿 ↓</a></div></div>
          <div><div className="demo-report-heading"><h3>成片评价</h3><span className="demo-tag">{sample.passed ? '所选项目已通过' : '仍有项目待处理'}</span></div><p className="demo-caption">来自本次任务已完成的评价报告。知识项为答题正确率，其余为最低页得分。</p>
            <div className="demo-scores">{sample.checks.map(check => <div key={check.key}><div><strong>{check.label}</strong><small>通过线 {check.threshold}%</small></div><span>{check.score}<small>%</small></span><div className="demo-score-track" role="meter" aria-label={check.label} aria-valuenow={check.score} aria-valuemin={0} aria-valuemax={100}><span style={{ width: `${check.score}%` }} /></div></div>)}</div>
            <details><summary>查看评价问题与待复核证据</summary><p className="demo-caption">通过评价仍可能存在改进项；ASR 与讲稿的差异仅为复核线索。</p>{sample.checks.flatMap(check => check.issues.map((issue, i) => <div className="demo-issue" key={`${check.key}-${i}`}><strong>第 {issue.page} 页 · {check.label}{issue.requires_review ? ' · 待人工复核' : ''}</strong><p>{issue.description}</p><blockquote>{issue.evidence}</blockquote></div>))}</details></div></div>
          <details className="demo-quiz"><summary>体验知识问答，看看这堂课讲清楚了什么</summary><p className="demo-caption">以下 3 题来自真实评价题库，作答仅用于体验，不改变原任务得分。</p>{sample.quiz.map((q, i) => <fieldset key={q.question}><legend>{i + 1}. {q.question}</legend><div>{Object.entries(q.options).map(([key, value]) => <label key={key}><input type="radio" name={`demo-question-${i}`} checked={answers[i] === key} onChange={() => setAnswers({ ...answers, [i]: key })} />{key}. {value}</label>)}</div>{answers[i] && <div className="demo-answer" role="status"><strong>{answers[i] === q.answer ? '回答正确' : `正确答案是 ${q.answer}`}</strong><p>原文依据：{q.source_quote}</p><p>成片证据：{q.evidence}</p></div>}</fieldset>)}</details>
        </>}

        <footer className="demo-panel-footer"><span>{step === 4 ? '体验结束，欢迎带着自己的素材开始。' : `下一步 · ${stages[step + 1][0]}`}</span><div>{step > 0 && <button className="demo-secondary" onClick={() => go(step - 1)}>上一步</button>}{step < 4 ? <button className="demo-primary" disabled={step === 2 && !accepted} onClick={() => { setLoaded(true); go(step + 1); }}>{step === 0 && !loaded ? '载入示例并继续' : step === 2 ? '确认并体验配音' : '继续体验'} →</button> : <>{fromPresentation && <a className="demo-secondary" href={presentationHref('teaching')}>继续进阶介绍 →</a>}<a className="demo-primary" href={workbenchHref}>用自己的素材创建任务 ↗</a></>}</div></footer>
      </section>
    </main>
    <footer className="demo-footer"><span>讲解工坊 · 从内容到讲解</span><span>示例来源 <code>{sample.job_id}</code> · {sample.created_at.slice(0, 10)}</span></footer>
  </div>;
}
