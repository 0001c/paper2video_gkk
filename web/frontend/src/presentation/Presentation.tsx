import { useEffect, useRef, useState } from 'react';
import { AccessControls } from '../AccessGate';
import { ThemeToggle } from '../ThemeToggle';
import { isPresentationSlide, presentationEntry, presentationSlides, type PresentationSlide } from '../presentationNavigation';
import sample from '../demo/sample.json';
import { notes } from './notes';

const paper = 'https://arxiv.org/abs/2510.05096';
const file = (path: string) => `/api/jobs/${sample.job_id}/files/${path}`;
const readIndex = () => {
  const id = window.location.hash.slice(1);
  return isPresentationSlide(id) ? presentationSlides.findIndex(slide => slide.id === id) : 0;
};

function WorkbenchLink({ slide }: { slide: PresentationSlide }) {
  return <a className="talk-primary" href={presentationEntry('/', slide)}>打开工作台 <span aria-hidden="true">↗</span></a>;
}

function SlideContent({ id, headingId = 'talk-title' }: { id: PresentationSlide; headingId?: string }) {
  switch (id) {
    case 'opening': return <div className="talk-cover">
      <div><p className="talk-overline">PAPER READING & PROJECT SHOWCASE</p><h1 id={headingId}>从论文，<br />到一堂可观看的课。</h1>
        <p className="talk-cover-sub">Paper2Video 论文解读<br /><span>与「讲解工坊」项目实践</span></p>
        <p className="talk-authors">参考论文：Zeyu Zhu · Kevin Qinghong Lin · Mike Zheng Shou<br />Show Lab, National University of Singapore · 2025</p></div>
      <div className="talk-cover-right"><span className="talk-margin-label">TODAY'S ROUTE</span><ol className="talk-route">
        <li><span>01</span><div><strong>理解论文</strong><p>研究问题、生成框架与评价</p></div><small>6 min</small></li>
        <li><span>02</span><div><strong>体验流程</strong><p>用真实产物走完一次制作</p></div><small>5 min</small></li>
        <li><span>03</span><div><strong>进入工作台</strong><p>教学控制与工程实现</p></div><small>7 min</small></li>
      </ol><p className="talk-small">参考节奏 20 分钟，含约 2 分钟讨论。</p><div className="talk-cover-mark" aria-hidden="true">Paper<br /><span>to Video.</span></div></div>
    </div>;
    case 'motivation': return <>
      <h1 id={headingId}>一份论文，<br />需要怎样的讲解？</h1><p className="talk-lead">从可阅读的文字，到可跟随的视听表达，<br className="talk-desktop-break" />中间需要完成一连串内容决策。</p>
      <ol className="talk-horizontal-list"><li><span>01 / 内容</span><h2>讲哪些重点</h2><p>长篇正文与密集图表<br />需要取舍和组织。</p></li><li><span>02 / 表达</span><h2>怎样讲清楚</h2><p>课件结构、口语讲稿<br />共同决定理解负担。</p></li><li><span>03 / 同步</span><h2>此刻看哪里</h2><p>声音、字幕与光标<br />需要指向同一处内容。</p></li></ol>
      <p className="talk-takeaway">研究目标：自动生成具备讲解结构与多模态同步的学术报告视频。</p>
    </>;
    case 'outputs': return <>
      <h1 id={headingId}>Paper2Video 与 PaperTalker</h1><p className="talk-lead">一个定义任务与评价基准，一个负责把论文变成视频。</p>
      <div className="talk-two-column talk-definition"><div><span className="talk-margin-label">TASK & BENCHMARK</span><h2>Paper2Video</h2><div className="talk-large-number">101<span>组配套材料</span></div><p>论文、作者报告视频与相关材料，<br />用于研究和评价自动生成的学术讲解。</p></div><div><span className="talk-margin-label">GENERATION FRAMEWORK</span><h2>PaperTalker</h2><p className="talk-definition-input">论文 + 声音参考 + 人像参考</p><div className="talk-output-words"><span>幻灯片</span><span>逐句字幕</span><span>语音讲解</span><span>讲解光标</span><span>讲者画面</span></div><p className="talk-small">讲者画面属于原框架能力。本次课程样例使用配音、字幕与光标。</p></div></div>
    </>;
    case 'pipeline': return <>
      <h1 id={headingId}>四个 Builder，连接内容与视听表达。</h1>
      <div className="talk-pipeline-layout"><figure className="talk-paper-figure"><img src="/presentation/papertalker-pipeline.png" alt="原论文图4：论文、声音与人像进入课件、字幕、光标和讲者四个Builder，输出多模态报告视频。" /><figcaption>原论文 Figure 4 · Overview of PaperTalker</figcaption></figure>
        <ol className="talk-builder-list"><li><span>01</span><div><h2>Slide Builder</h2><p>生成 Beamer 课件，编译与修正版面。</p></div></li><li><span>02</span><div><h2>Subtitle Builder</h2><p>看逐页画面，组织讲稿与视觉目标。</p></div></li><li><span>03</span><div><h2>Cursor Builder</h2><p>确定光标位置与出现时间。</p></div></li><li><span>04</span><div><h2>Talk Builder</h2><p>生成语音与讲者画面，组合成片。</p></div></li></ol></div>
    </>;
    case 'alignment': return <>
      <h1 id={headingId}>版面、讲解与指向，如何对齐？</h1><div className="talk-two-column talk-methods"><div><span className="talk-margin-label">LAYOUT / 版面</span><h2>先渲染候选，再做视觉选择。</h2><p>Tree Search Visual Choice：调整字号和图像尺寸，<br className="talk-desktop-break" />渲染多个候选，用视觉模型选择布局。</p><figure className="talk-paper-figure talk-tree"><img src="/presentation/tree-search.png" alt="原论文图5：将调整图像尺寸后的布局候选拼接，由视觉语言模型选择合适版面。" /><figcaption>原论文 Figure 5 · Tree Search Visual Choice</figcaption></figure></div>
        <div><span className="talk-margin-label">GROUNDING / 指向</span><h2>同一句话，同时对齐两个维度。</h2><div className="talk-alignment-row"><span>空间</span><div><strong>UI-TARS</strong><p>根据视觉目标定位课件上的位置。</p></div></div><div className="talk-alignment-row"><span>时间</span><div><strong>WhisperX</strong><p>根据词级时间，让光标跟上语音。</p></div></div><p className="talk-method-note">效率补充：按页并行生成。论文报告超过 6 倍提速，来自其多 GPU 实验环境。</p></div></div>
    </>;
    case 'evaluation': return <>
      <h1 id={headingId}>问答指标提升，仍需完整看待质量。</h1><div className="talk-evaluation-layout"><div><p className="talk-lead">评价覆盖内容、偏好、理解与记忆。</p><dl className="talk-metrics"><dt>Meta Similarity</dt><dd>语音与内容等相似性</dd><dt>PresentArena</dt><dd>代理观众对视频的成对偏好</dd><dt>PresentQuiz</dt><dd>VideoLLM 观看视频后作答</dd><dt>IP Memory</dt><dd>对作者与工作的回忆</dd></dl></div>
        <div><p className="talk-table-caption">PresentQuiz · 原论文 Table 2</p><table className="talk-table"><thead><tr><th scope="col">视频来源</th><th scope="col">细节问答</th><th scope="col">理解问答</th></tr></thead><tbody><tr><th scope="row">作者视频</th><td>73.8%</td><td>90.8%</td></tr><tr><th scope="row">PaperTalker</th><td><strong>84.2%</strong></td><td><strong>95.1%</strong></td></tr></tbody></table><p className="talk-evidence-note">人评均分：作者视频 4.6 / PaperTalker 3.8<br /><span>原论文 Figure 6 · 5 分制</span></p></div></div>
      <p className="talk-takeaway">PresentQuiz 测量代理模型的问答表现，真实学生的学习效果仍需单独验证。</p>
    </>;
    case 'bridge': return <>
      <h1 id={headingId}>从论文方法，到可操作的教学工作流。</h1><p className="talk-lead">沿用生成与对齐思路，进一步让使用者控制“教什么、怎么教、何时继续”。</p>
      <div className="talk-two-column talk-bridge"><div><span className="talk-margin-label">参考论文的贡献</span><h2>多通道报告生成</h2><ul><li>课件、讲稿、语音和视觉引导的协作</li><li>版面优化与时空对齐方法</li><li>多维度评价自动生成视频</li></ul></div><div><span className="talk-margin-label">本项目的实践与扩展</span><h2>面向课程制作的工作台</h2><ul><li>PDF、Word、LaTeX 素材输入与预览</li><li>教学目标、范围、时间与逐页审核</li><li>模型配置、评价复核与产物复用</li></ul></div></div>
      <p className="talk-small talk-boundary">本次真实样例：已生成 20 页课件、配音、光标、字幕与成片；未启用树搜索排版和数字人。</p>
    </>;
    case 'demo': return <div className="talk-demo-layout"><div><span className="talk-margin-label">LIVE DEMO / 约 5 分钟</span><h1 id={headingId}>走进一份<br />真实生成的课程。</h1><p className="talk-lead">{sample.title}</p><div className="talk-demo-facts"><span><strong>20</strong>页课件</span><span><strong>23:26</strong>完整成片</span><span><strong>5</strong>步体验</span></div><a className="talk-primary" href={presentationEntry('/demo/', 'demo')}>进入流程体验 <span aria-hidden="true">→</span></a><p className="talk-small">已有产物随时可看，体验操作不触发模型生成。</p></div><div><figure className="talk-demo-preview"><img src={file(sample.pages[0].slide)} alt="真实课程封面：大语言模型的构建过程与扩展法则" /><figcaption>样例任务 · {sample.job_id}</figcaption></figure><ol className="talk-demo-cues"><li>看教学安排，再选第 10 页核对课件与讲稿。</li><li>试听一页配音，播放约 45–60 秒成片。</li><li>查看评价证据，继续进入工作台进阶。</li></ol></div></div>;
    case 'teaching': return <>
      <h1 id={headingId}>把教学意图，写进生成过程。</h1><p className="talk-lead">同一份教材，面对不同的学习者，需要不同的讲法。</p>
      <ol className="talk-horizontal-list"><li><span>输入前</span><h2>确定对象与目标</h2><p>教学对象、重点、难点<br />约束内容深度与讲解方式。</p></li><li><span>生成前</span><h2>确认范围与教案</h2><p>选章节、分配时间，<br />先确认课程组织再继续。</p></li><li><span>合片前</span><h2>核对课件与讲稿</h2><p>逐页检查表达和依据，<br />结合评价决定下一步。</p></li></ol>
      <div className="talk-action-row"><WorkbenchLink slide="teaching" /><p>演示重点：教学范围 → 教案预览 → 审核流程<br /><span>返回时，点击工作台右上角“返回汇报”。</span></p></div>
    </>;
    case 'configuration': return <>
      <h1 id={headingId}>按环节配置模型与资源。</h1><p className="talk-lead">把能力、质量和资源取舍，落实到每一段制作流程。</p>
      <div className="talk-config-rows"><div><span>01 / 模型</span><h2>按职责选择</h2><p>正文处理、课件讲稿、视觉定位与评价各有分工。</p></div><div><span>02 / 声音</span><h2>按讲解选择</h2><p>管理可用音色，结合语言与讲稿试听效果。</p></div><div><span>03 / 画面</span><h2>按需要启用</h2><p>配置字幕和光标；数字人需要额外生成资源。</p></div></div>
      <div className="talk-action-row"><WorkbenchLink slide="configuration" /><p>演示重点：模型配置 → 音色 → 光标与字幕<br /><span>成本和耗时取决于所选模型、页数与运行环境。</span></p></div>
    </>;
    case 'reliability': return <>
      <h1 id={headingId}>结果可检查，问题可复核。</h1><div className="talk-two-column talk-reliability"><div><p className="talk-lead">从“生成完成”，走向“有依据地验收”。</p><ol className="talk-recovery"><li><strong>评价定位问题</strong><p>知识、课件、讲述与音频分别检查。</p></li><li><strong>查看原始证据</strong><p>结合页码和内容复核，避免只看总分。</p></li><li><strong>按范围恢复</strong><p>续跑前确认覆盖范围，按内容校验复用有效产物。</p></li></ol></div><div><p className="talk-table-caption">本次真实任务 · 已有评价快照</p><div className="talk-sample-scores">{sample.checks.map(check => <div key={check.key}><span>{check.label}</span><strong>{check.score}<small>%</small></strong></div>)}</div><p className="talk-small">知识项为答题正确率，其余为最低页得分。<br />通过线是检查条件，仍可能留下待复核问题。</p></div></div>
      <div className="talk-action-row"><WorkbenchLink slide="reliability" /><p>演示重点：评估配置 → 任务记录 → 复核与续跑范围</p></div>
    </>;
    case 'closing': return <>
      <span className="talk-margin-label">DISCUSSION / 约 2 分钟</span><h1 id={headingId}>从自动生成，<br />走向可控制、可检查的课程制作。</h1><div className="talk-two-column talk-closing"><div><h2>已经完成</h2><ul><li>素材、教案、课件、讲稿到成片的制作流程</li><li>可体验的真实样例与逐页产物</li><li>教学控制、审核、评价与恢复入口</li></ul></div><div><h2>下一步验证</h2><ul><li>用更多教材与复杂版面验证稳定性</li><li>通过真实学习者实验评估教学效果</li><li>记录可复现的运行时长与成本</li></ul></div></div><div className="talk-action-row"><WorkbenchLink slide="closing" /><a className="talk-link" href={paper} target="_blank" rel="noreferrer">阅读参考论文 ↗</a><span className="talk-small">欢迎提问与讨论。</span></div>
    </>;
  }
}

const sources = [
  'Paper2Video · arXiv:2510.05096v2 · 2025-10-09', '原论文 §1 Introduction', '原论文 §3 Benchmark、§4 PaperTalker',
  '原论文 §4、Figure 4', '原论文 §4.1、§4.4、Figure 5、Table 3', '原论文 §3.2、Table 2、Figure 6',
  '论文方法与本项目功能对照', `本项目真实任务 ${sample.job_id}`, '本项目工作台 · 教学范围与生成审核',
  '本项目工作台 · 模型与生成配置', '本项目评价快照与任务恢复流程', '本项目实现与后续验证计划',
];

export default function Presentation() {
  const [{ index, previous, direction }, setSlide] = useState(() => ({ index: readIndex(), previous: null as number | null, direction: 1 }));
  const [directory, setDirectory] = useState(false);
  const [showNotes, setShowNotes] = useState(false);
  const [fullscreen, setFullscreen] = useState(!!document.fullscreenElement);
  const [notice, setNotice] = useState('');
  const heading = useRef<HTMLElement>(null);
  const slide = presentationSlides[index];
  function go(next: number) {
    const safe = Math.max(0, Math.min(presentationSlides.length - 1, next));
    if (safe === index) return;
    window.location.hash = presentationSlides[safe].id;
    setDirectory(false);
  }
  async function toggleFullscreen() {
    try {
      if (document.fullscreenElement) await document.exitFullscreen();
      else if (document.documentElement.requestFullscreen) await document.documentElement.requestFullscreen();
      else setNotice('当前浏览器不支持页面全屏，可使用浏览器自带的全屏功能。');
    } catch { setNotice('未能进入全屏，可使用浏览器自带的全屏功能。'); }
  }
  useEffect(() => {
    const update = () => {
      const next = readIndex();
      const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
      setSlide(current => next === current.index ? current : {
        index: next, previous: reducedMotion ? null : current.index, direction: next > current.index ? 1 : -1,
      });
      setDirectory(false);
    };
    const full = () => setFullscreen(!!document.fullscreenElement);
    window.addEventListener('hashchange', update);
    document.addEventListener('fullscreenchange', full);
    return () => { window.removeEventListener('hashchange', update); document.removeEventListener('fullscreenchange', full); };
  }, []);
  useEffect(() => {
    if (previous === null) return;
    // 快速连续翻页会取消旧清理任务，只留下当前一次过渡。
    const cleanup = window.setTimeout(() => setSlide(current => ({ ...current, previous: null })), 280);
    return () => window.clearTimeout(cleanup);
  }, [index, previous]);
  useEffect(() => {
    document.title = `${slide.title} · 论文与项目汇报`;
    heading.current?.focus({ preventScroll: true });
    window.scrollTo({ top: 0 });
  }, [index, slide.title]);
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') { setDirectory(false); setShowNotes(false); return; }
      if (event.altKey || event.ctrlKey || event.metaKey || event.target instanceof HTMLElement && event.target.closest('input, textarea, select, button, a, [contenteditable="true"], [role="dialog"]')) return;
      if (directory) return;
      if (['ArrowRight', 'ArrowDown', 'PageDown', ' '].includes(event.key)) { event.preventDefault(); go(index + 1); }
      else if (['ArrowLeft', 'ArrowUp', 'PageUp'].includes(event.key)) { event.preventDefault(); go(index - 1); }
      else if (event.key === 'Home') { event.preventDefault(); go(0); }
      else if (event.key === 'End') { event.preventDefault(); go(presentationSlides.length - 1); }
      else if (event.key.toLowerCase() === 'n') setShowNotes(value => !value);
      else if (event.key.toLowerCase() === 'f') { event.preventDefault(); void toggleFullscreen(); }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  });

  return <div className="talk-shell">
    <header className="talk-header"><a className="brand" href="/presentation/#opening">讲解工坊<span className="brand-caption">PAPER2VIDEO</span></a><div className="talk-header-actions"><AccessControls /><ThemeToggle /><button className="talk-utility" onClick={() => void toggleFullscreen()}>{fullscreen ? '退出全屏' : '全屏'} <span aria-hidden="true">⛶</span></button></div></header>
    <nav className="talk-chapters" aria-label="汇报章节">{['论文', '流程体验', '工作台进阶'].map((section, i) => <button key={section} aria-current={slide.section === section ? 'step' : undefined} onClick={() => go(presentationSlides.findIndex(item => item.section === section))}><span>0{i + 1}</span>{section}</button>)}<span className="talk-chapter-caption">论文阅读 / 项目实践</span></nav>
    <main><div className="talk-page-heading"><span>{slide.section} <i>/</i> {String(index + 1).padStart(2, '0')}</span><button className="talk-utility" aria-expanded={directory} aria-controls="talk-directory" onClick={() => setDirectory(value => !value)}>汇报目录 {directory ? '−' : '+'}</button></div>
      {directory && <nav id="talk-directory" className="talk-directory" aria-label="幻灯片目录">{presentationSlides.map((item, i) => <a key={item.id} href={`#${item.id}`} aria-current={index === i ? 'page' : undefined} onClick={() => { setDirectory(false); if (index === i) heading.current?.focus(); }}><span>{String(i + 1).padStart(2, '0')}</span>{item.title}</a>)}</nav>}
      <div className="talk-stage" data-direction={direction} data-transitioning={previous === null ? undefined : 'true'}>
        {previous !== null && <div className="talk-slide talk-slide-outgoing" key={`outgoing-${previous}`} aria-hidden="true" {...{ inert: '' }}>
          <SlideContent id={presentationSlides[previous].id} headingId="talk-outgoing-title" />
        </div>}
        <article className={`talk-slide talk-slide-active talk-slide-${slide.id}`} ref={heading} tabIndex={-1} aria-labelledby="talk-title" key={slide.id}><SlideContent id={slide.id} /></article>
      </div>
      <footer className="talk-slide-footer"><span>{index <= 5 ? <a href={paper} target="_blank" rel="noreferrer">{sources[index]} ↗</a> : sources[index]}</span><span>讲解工坊 · 论文与项目汇报</span></footer>
      <div className="talk-controls"><div className="talk-progress" aria-hidden="true">{presentationSlides.map((item, i) => <span key={item.id} className={i <= index ? 'is-past' : undefined} />)}</div><div className="talk-control-row"><button className="talk-utility" aria-expanded={showNotes} aria-controls="talk-notes" onClick={() => setShowNotes(value => !value)}>{showNotes ? '收起' : '演讲'}备注 <span className="talk-key">N</span></button><span className="talk-keyboard-hint">← → 翻页 <i>·</i> F 全屏</span><div className="talk-pagination"><button className="talk-utility" disabled={index === 0} onClick={() => go(index - 1)} aria-label="上一页幻灯片">←</button><span aria-live="polite" aria-atomic="true">{String(index + 1).padStart(2, '0')} <i>/</i> {presentationSlides.length}</span><button className="talk-next" disabled={index === presentationSlides.length - 1} onClick={() => go(index + 1)} aria-label="下一页幻灯片">下一页 →</button></div></div></div>
      {showNotes && <aside id="talk-notes" className="talk-notes" aria-label="当前页演讲备注"><strong>{slide.title} · 演讲提示</strong><p>{notes[index]}</p><span>备注在当前页面展开，投影时可收起。</span></aside>}
      {notice && <p className="talk-notice" role="status">{notice}</p>}
    </main>
  </div>;
}
