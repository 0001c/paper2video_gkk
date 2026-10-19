"""Read-only smoke checks for the built /demo page and its original media."""
import argparse
import hashlib
import json
import re
from pathlib import Path
from urllib.parse import urljoin

import requests

ROOT = Path(__file__).resolve().parents[1]


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--base-url', default='http://127.0.0.1:8848')
    args = parser.parse_args()
    sample = read(ROOT / 'web/frontend/src/demo/sample.json')
    job_dir = ROOT / 'jobs' / sample['job_id']
    job = read(job_dir / 'job.json')
    result = Path(job['config']['result_dir'])
    report = read(job_dir / 'qa-video.json')
    timing = read(result / 'course_timing.json')
    plan = read(job['config']['course_plan_path'])
    originals = [job_dir / 'job.json', job_dir / 'qa-video.json', result / '3_merage.mp4', result / 'subtitle_w_cursor.txt']
    digest = lambda p: hashlib.sha256(p.read_bytes()).hexdigest()
    before = {str(p): digest(p) for p in originals}
    checks = 0

    def check(condition, label):
        nonlocal checks
        assert condition, label
        checks += 1

    check(sample['plan']['sections'] == plan['sections'], 'Source sections must match the historical course plan')
    check(len(sample['pages']) == len(timing['pages']) == 20, 'Twenty pages must retain their indices')
    scripts = [p.strip() for p in (result / 'subtitle_w_cursor.txt').read_text(encoding='utf-8').split('###') if p.strip()]
    elapsed = 0
    for i, page in enumerate(sample['pages']):
        check(page['page'] == i + 1 and page['slide'] == f'slide_imgs/{i + 1}.png' and page['audio'] == f'audio/{i}.wav', f'Page {i + 1} media alignment')
        check(page['script'] == [line.split('|', 1)[0].strip() for line in scripts[i].splitlines() if '|' in line], f'Page {i + 1} script fidelity')
        check(page['start_seconds'] == round(elapsed, 2), f'Page {i + 1} start time')
        elapsed += timing['pages'][i]['actual_seconds']
    check(sample['duration_seconds'] == round(elapsed, 2), 'Total audio duration')
    for item in sample['checks']:
        actual = report['checks'][item['key']]
        check(all(item[k] == actual[k] for k in ('label', 'score', 'threshold', 'passed')), f'{item["key"]} actual score and threshold')
    check(sample['passed'] == report['passed'], 'Actual overall outcome')
    check(not re.search(r'[A-Z]:\\|api_key|connection_id|ref_audio|model_config|source_path', json.dumps(sample)), 'No credentials, connection metadata or local paths in demo snapshot')

    session = requests.Session()
    session.trust_env = False
    response = session.get(args.base_url + '/demo', timeout=20)
    check(response.status_code == 200 and response.url.endswith('/demo/'), '/demo redirect and built entry')
    check('src/demo/main.tsx' not in response.text and '<title>' in response.text, 'Serve production build, not source')
    for asset in re.findall(r'(?:src|href)="([^"]+\.(?:js|css))"', response.text):
        asset_response = session.get(urljoin(response.url, asset), timeout=20)
        check(asset_response.status_code == 200 and len(asset_response.content) > 0, f'Nested demo asset URL {asset}')
    paths = [p[key] for p in sample['pages'] for key in ('slide', 'audio')]
    paths += ['3_merage.mp4', 'slides_rendered.pdf', 'subtitle_w_cursor.txt']
    for path in paths:
        with session.get(f'{args.base_url}/api/jobs/{sample["job_id"]}/files/{path}', headers={'Range': 'bytes=0-31'}, timeout=20, stream=True) as media:
            check(media.status_code == 206 and media.headers.get('Content-Range', '').startswith('bytes 0-31/'), f'Range support: {path}')
            check(media.raw.read(32) == (result / path).read_bytes()[:32], f'Original bytes: {path}')
    check(session.get(args.base_url + '/', timeout=20).status_code == 200, 'Existing workbench still serves')
    check(before == {str(p): digest(p) for p in originals}, 'Original job, report, script and video are unchanged')
    evidence = ROOT / 'jobs/_validation/demo-2026-10-19'
    evidence.mkdir(parents=True, exist_ok=True)
    (evidence / 'smoke.json').write_text(json.dumps(dict(checks=checks, passed=True, hashes=before), ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'PASS: {checks} checks; snapshot fidelity, production entry/assets, 43 media URLs, Range and original-file integrity')


if __name__ == '__main__':
    main()
