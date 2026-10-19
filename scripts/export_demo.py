"""Export a whitelisted, read-only demo snapshot; media stays in the original job."""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
JOB_ID = 'job_1019_192023_1092'


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def main():
    job_dir = ROOT / 'jobs' / JOB_ID
    job = read(job_dir / 'job.json')
    result = Path(job['config']['result_dir'])
    plan = read(job['config']['course_plan_path'])
    teaching = read(result / 'teaching_plan.json')
    timing = read(result / 'course_timing.json')
    report = read(job_dir / 'qa-video.json')
    scripts = [p.strip() for p in (result / 'subtitle_w_cursor.txt').read_text(encoding='utf-8').split('###') if p.strip()]
    assert len(scripts) == len(teaching['pages']) == len(timing['pages'])
    pages = []
    elapsed = 0
    for i, (script, page, duration) in enumerate(zip(scripts, teaching['pages'], timing['pages'])):
        slide, audio = f'slide_imgs/{i + 1}.png', f'audio/{i}.wav'
        assert (result / slide).is_file() and (result / audio).is_file()
        lines = [line.split('|', 1) for line in script.splitlines() if '|' in line]
        seconds = duration['actual_seconds']
        pages.append(dict(page=i + 1, title=page['title'], slide=slide, audio=audio,
                          script=[line[0].strip() for line in lines],
                          cursors=[line[1].strip() for line in lines],
                          seconds=seconds, start_seconds=round(elapsed, 2)))
        elapsed += seconds
    checks = []
    for key, check in report['checks'].items():
        checks.append(dict(key=key, **{k: check[k] for k in ('label', 'score', 'threshold', 'passed')},
                           metric=check.get('metric', 'knowledge_accuracy'),
                           issues=[{k: issue.get(k) for k in ('page', 'description', 'evidence', 'requires_review')}
                                   for issue in check.get('issues', [])] if isinstance(check.get('issues'), list) else []))
    snapshot = dict(job_id=JOB_ID, source_name=job['source_name'], created_at=job['created_at'],
                    title=pages[0]['title'], duration_seconds=round(elapsed, 2),
                    plan={k: plan[k] for k in ('audience', 'objectives', 'focus', 'depth', 'planned_minutes', 'sections')},
                    pages=pages, chapters=read(result / 'chapters.json'), checks=checks,
                    passed=report['passed'],
                    quiz=[{k: item[k] for k in ('question', 'options', 'answer', 'source_quote', 'evidence')}
                          for item in report['checks']['video']['items'][:3]])
    output = ROOT / 'web/frontend/src/demo/sample.json'
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(f'Exported {len(pages)} pages and {len(checks)} checks to {output.relative_to(ROOT)}')


if __name__ == '__main__':
    main()
