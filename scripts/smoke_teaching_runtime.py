"""在已启动的本机后端验证教学审核；只复制冻结材料，不启动生成或配音。"""
import hashlib
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
FROZEN = ROOT / 'jobs/_validation/nlp_ch3_2026-10-15'
EVIDENCE = ROOT / 'jobs/_validation/teaching-runtime-2026-10-17'
BASE = 'http://127.0.0.1:8848'
sys.path.insert(0, str(ROOT / 'Paper2Video-main/src'))
import course


def fingerprint(paths):
    return {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in paths if path.is_file()}


def main():
    session = requests.Session()
    session.trust_env = False
    checks = []

    def check(condition, description):
        if not condition:
            raise AssertionError(description)
        checks.append(description)
        print('通过：' + description)

    def api(method, path, body=None, status=200):
        # 此脚本永远不调用生成、配音、评价或数字人端点。
        assert method == 'GET' or (method == 'PUT' and path == route)
        response = session.request(method, BASE + path, json=body, timeout=30)
        assert response.status_code == status, (path, response.status_code, response.text[:500])
        return response.json()

    jobs = api('GET', '/api/jobs')['jobs']
    check(not any(job['status'] in ('queued', 'running', 'preparing') for job in jobs), '后端没有活动任务')
    connections = api('GET', '/api/model-connections')['items']
    check(all('api_key' not in row for row in connections), '公开连接接口不包含密钥')
    check(all(row.get('key_present') for row in connections if row.get('persist_key')), '已保存连接的密钥恢复可用')
    history_paths = [ROOT / 'jobs' / job['id'] / 'job.json' for job in jobs]
    frozen_paths = [FROZEN / 'plan.json', FROZEN / 'B1/subtitle_w_cursor.txt',
                    FROZEN / 'common_slides.pdf', *sorted((FROZEN / 'common_slides').glob('*.png'))]
    before = fingerprint(history_paths + frozen_paths)
    job_id = 'job_smoke_teaching_' + datetime.now().strftime('%m%d_%H%M%S_%f')
    work = ROOT / 'jobs' / job_id
    result = work / 'result'
    result.mkdir(parents=True)
    shutil.copytree(FROZEN / 'common_slides', result / 'slide_imgs')
    shutil.copy2(FROZEN / 'common_slides.pdf', result / 'slides_rendered.pdf')
    shutil.copy2(FROZEN / 'B1/subtitle_w_cursor.txt', result / 'subtitle_w_cursor.txt')
    shutil.copy2(FROZEN / 'plan.json', work / 'course_plan.json')
    job = {
        'id': job_id, 'created_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'status': 'awaiting_review', 'phase': '运行态冒烟：冻结材料副本，未调用模型',
        'phase_key': 'review', 'source_name': '[运行态冒烟] 第三章教学规划与张量口播',
        'config': {'script_lang': 'zh', 'slide_lang': 'zh', 'generate_talking': False,
                   'review_before_render': True, 'course_plan_path': str(work / 'course_plan.json'),
                   'stages': ['2'], 'cursor_style': 'none', 'cursor_scale': 1},
        'validation_fixture': {'source': str(FROZEN.relative_to(ROOT)), 'variant': 'B1',
                               'purpose': '审核接口与页面集成验证，不代表新版真实生成质量'},
    }
    (work / 'job.json').write_text(json.dumps(job, ensure_ascii=False, indent=2), encoding='utf-8')
    route = '/api/jobs/' + job_id + '/review'
    review = api('GET', route)
    check(review['editable'] and len(review['pages']) == 8, '真实审核接口读取八页冻结材料副本')
    check(len(review['teaching_tasks']) == 8 and all(t['knowledge'] for t in review['teaching_tasks']),
          '八页教学任务包含具体知识项')
    ledger = review['teaching']['lesson_ledger']
    check(len(ledger['pages']) == 8 and all(p['knowledge'] for p in ledger['pages']), '审核返回逐页知识清单')
    check(any(q['answer_page'] and '待核对' in q['status'] for q in ledger['pending_questions']),
          '跨页承诺保留负责页和待核对状态')
    contracts = review['teaching']['tensor_contracts']
    check(any(c['symbols'].get('x') == [1, 10] and c['symbols'].get('W') == [10, 5]
              and c['symbols'].get('b') == [5] for c in contracts), '整课形状含教材 x、W、b 约束')
    check(not review['teaching']['passed'] and any('内维' in i for i in review['teaching']['issues']),
          '历史反向乘法理由错误在真实接口检出，保持审核阻断')
    check(len(review['oral_previews']) == 8, '每页均返回实际口播预览')
    check(len(review['version']) == 64, '审核令牌包含新版规划与口播规范')
    body = {'version': review['version'], 'pages': review['pages']}
    api('PUT', route, {**body, 'unknown': True}, status=422)
    api('PUT', route, {**body, 'pages': body['pages'][:-1]}, status=422)
    check(api('GET', route)['version'] == review['version'], '错误写入拒绝且不改变讲稿')
    edited = list(review['pages'])
    edited[4] += '\n口播映射核对：tf.Variable、tf.nn.sigmoid、feed_dict、w_1。 | no'
    saved = api('PUT', route, {**body, 'pages': edited})
    check(saved['version'] != review['version'] and saved['pages'] == edited, '保存完整讲稿后刷新审核版本')
    check(all(word in saved['oral_previews'][4] for word in
              ('tf点Variable', 'tf点nn点sigmoid', 'feed下划线dict', 'w下标一')), '保存后更新代码点、下划线及数字下标的实际读法')
    check(len(list((result / 'script_versions').glob('*.txt'))) == 1, '保存前的冻结讲稿副本保留历史版本')
    api('PUT', route, body, status=409)
    check(api('GET', route)['version'] == saved['version'], '旧审核令牌返回409，不能覆盖新稿')
    for page in (1, 5, 6, 8):
        response = session.get(BASE + '/api/jobs/' + job_id + f'/files/slide_imgs/{page}.png', timeout=10)
        check(response.status_code == 200 and response.headers['content-type'].startswith('image/'),
              f'第{page}页课件可通过真实文件接口展示')
    current_jobs = api('GET', '/api/jobs')['jobs']
    check({j['id'] for j in current_jobs} == {j['id'] for j in jobs} | {job_id}, '仅新增冒烟副本，没有启动下游生成任务')
    check(fingerprint(history_paths + frozen_paths) == before, '原任务记录和冻结输入哈希保持不变')
    check(api('GET', '/api/model-connections')['items'] == connections, '原连接公开元数据与密钥存在状态保持不变')
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    for name, value in [('review-before.json', review), ('review-after.json', saved), ('input-hashes.json', before)]:
        (EVIDENCE / name).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    summary = {'base_url': BASE, 'job_id': job_id, 'historical_jobs': len(jobs), 'checks': checks,
               'connections': [{k: c.get(k) for k in ('id', 'provider', 'source', 'key_present', 'persist_key')} for c in connections],
               'model_calls': 0, 'avatar_calls': 0, 'fixture_augmented_page': 5}
    (EVIDENCE / 'verification.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'job_id': job_id, 'passed': len(checks), 'evidence': str(EVIDENCE)}, ensure_ascii=False))


if __name__ == '__main__':
    main()
