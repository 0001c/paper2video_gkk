import { useEffect, useRef, useState } from 'react';
import { Button } from 'tdesign-react';
import { api, errorMessage, runExclusive } from './flow';

type Source = { text: string; review_pages: number[]; pages: { page: number; url: string }[] };
export function SourceReview({ ingestId, onSaved, preservedGlyphs = 0 }: { ingestId: string; onSaved: (data: any, file: File) => void; preservedGlyphs?: number }) {
  const [source, setSource] = useState<Source>();
  const [text, setText] = useState('');
  const [page, setPage] = useState(0);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState('');
  const lock = useRef(false);
  const version = useRef(0);
  useEffect(() => { version.current++; return () => { version.current++; }; }, [ingestId]);
  const open = () => runExclusive(lock, setBusy, async () => {
    const current = version.current;
    const data = await api.get(`/api/ingest/${ingestId}/source`);
    if (current !== version.current) return;
    setSource(data); setText(data.text); setPage(data.pages[0]?.page ?? 0); setMessage('');
  }, (error) => setMessage(errorMessage(error)));
  const unresolved = text.includes('【未识别字形】') || /[\u0000-\u0008\u000b\u000c\u000e-\u001f\ufffd]/.test(text);
  return <div className="source-review">
    <Button variant="outline" loading={busy} disabled={busy} onClick={open}>{preservedGlyphs ? '查看保留的公式与源页' : '对照源页修正正文'}</Button>
    {source && <>
      <p className="hint">{preservedGlyphs ? '公式和特殊字形已保留原图，可以直接生成。如编辑正文，请保留原文字形图标记，或对照源页完整转写。' : '逐处对照源页补齐文字和公式。'}保存会生成一份关联原文件及图示的新素材；随后重新确认教学范围与教案，再创建任务。</p>
      <div className="source-review-grid">
        <div className="field"><label htmlFor={`source-text-${ingestId}`}>完整正文 · {text.length} 字符</label>
          <textarea id={`source-text-${ingestId}`} value={text} disabled={busy} onChange={(event) => setText(event.target.value)} />
        </div>
        <div className="field"><label htmlFor={`source-page-${ingestId}`}>源页证据</label>
          {source.pages.length ? <><select id={`source-page-${ingestId}`} value={page} onChange={(event) => setPage(Number(event.target.value))}>
            {source.pages.map((item) => <option key={item.page} value={item.page}>源文第 {item.page} 页</option>)}
          </select><a href={source.pages.find((item) => item.page === page)?.url} target="_blank" rel="noreferrer">
            <img src={source.pages.find((item) => item.page === page)?.url} alt={`源文第 ${page} 页，点击查看原尺寸`} />
          </a></> : <p className="hint">没有归档的源页图片，请对照原文件。</p>}
        </div>
      </div>
      {unresolved && <p className="notice warn" role="status">正文仍含未识别字形。请逐处修正后再保存，不能通过删除缺失标记代替补齐公式。</p>}
      <Button disabled={busy || unresolved || !text.trim() || text === source.text} onClick={() => runExclusive(lock, setBusy, async () => {
        const current = version.current;
        const file = new File([text], '修正正文.txt', { type: 'text/plain' });
        const { data } = await api.post(`/api/ingest/${ingestId}/review`, { text });
        if (current !== version.current) return;
        onSaved(data, file); setSource(undefined); setMessage('修正正文已作为新素材保存，请重新确认教案。');
      }, (error) => setMessage(errorMessage(error)))}>保存修正正文为新素材</Button>
      <Button variant="text" disabled={busy} onClick={() => setSource(undefined)}>收起</Button>
    </>}
    {message && <p className="notice info" role="status">{message}</p>}
  </div>;
}
