"""讲稿并发、恢复、进度及模型选择回归；只用本地虚构接口，不调用真实模型/GPU。"""
import importlib.util
import asyncio
import json
import os
import tempfile
import threading
import time
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('script_models_web', ROOT / 'web/app.py')
web = importlib.util.module_from_spec(spec)
spec.loader.exec_module(web)
import course
import progress_report


def main():
    with tempfile.TemporaryDirectory(prefix='.script-models-', dir=ROOT) as directory:
        tmp = Path(directory)
        web.JOBS_ROOT = str(tmp / 'jobs')
        web._QUEUE = asyncio.Queue()
        Path(web.JOBS_ROOT).mkdir()
        job_id = 'job_test'
        job_dir = Path(web.JOBS_ROOT, job_id)
        job_dir.mkdir()
        progress = job_dir / 'progress.jsonl'
        old_progress = os.environ.get('PAPER2VIDEO_PROGRESS_FILE')
        os.environ['PAPER2VIDEO_PROGRESS_FILE'] = str(progress)
        try:
            slides = tmp / 'slides'
            slides.mkdir()
            result = tmp / 'result'
            result.mkdir()
            for i in range(4):
                (slides / f'{i + 1}.png').write_bytes(b'fake slide')
            prompt = tmp / 'prompt.txt'
            prompt.write_text(web._script_prompt_text('textbook', 'zh'), encoding='utf-8')
            barrier = threading.Barrier(3)
            lock = threading.Lock()
            active = peak = 0
            completion, calls = [], []
            def generator(*args, **kwargs):
                nonlocal active, peak
                i = int(Path(kwargs['image_paths'][0]).stem) - 1
                with lock:
                    active += 1
                    peak = max(peak, active)
                    calls.append(i)
                if i < 3:
                    barrier.wait(timeout=3)
                time.sleep(0.05 if i == 0 else 0.005)
                with lock:
                    active -= 1
                    completion.append(i)
                assert '课件路线' in kwargs['prompt_text']
                return f'知识讲解 {i}。 | no', {'total_tokens': 1}
            output, _ = course.generate_script(None, slides, prompt, {'id': 'fake'}, result, generator, workers=3)
            assert peak == 3 and completion[0] != 0
            assert course.split_pages(output) == [f'知识讲解 {i}。 | no' for i in range(4)]
            assert [r['done'] for r in progress_report.read_all(progress)] == [0, 1, 2, 3, 4]
            course.generate_script(None, slides, prompt, {'id': 'fake'}, result, generator, workers=3)
            assert Counter(calls) == Counter(range(4)), '缓存恢复不得重复生成成功页'
            Path(result, 'subtitle_w_cursor.txt').write_text(output, encoding='utf-8')
            try:
                course.generate_selected(slides, prompt, {}, result, lambda *a, **k: ('第一段 | no\n###\n第二段 | no', {}), [1])
                raise AssertionError('多页输出未拒绝')
            except ValueError:
                assert Path(result, 'subtitle_w_cursor.txt').read_text(encoding='utf-8') == output
            failed_dir = tmp / 'failed'
            failure_calls = []
            fail = True
            def failing(*args, **kwargs):
                i = int(Path(kwargs['image_paths'][0]).stem) - 1
                failure_calls.append(i)
                if i == 1 and fail:
                    raise RuntimeError('模拟单页失败')
                return f'解释 {i}。 | no', {}
            try:
                course.generate_script(None, slides, prompt, {}, failed_dir, failing, workers=1)
                raise AssertionError('单页失败未报告')
            except RuntimeError:
                saved = {int(p.stem) for p in (failed_dir / 'course_script').glob('*.json')}
                assert 0 in saved
            fail = False
            failure_calls.clear()
            recovered, _ = course.generate_script(None, slides, prompt, {}, failed_dir, failing, workers=2)
            assert saved.isdisjoint(failure_calls) and len(course.split_pages(recovered)) == 4

            progress.unlink()
            progress_report.report('script', stage=2, done=0, total=30, total_pages=30)
            progress_report.report('script', stage=2, done=27, total=30, total_pages=30, generated_done=27, generation_elapsed_s=810, concurrency=3)
            job = {'id': job_id, 'status': 'running', 'phase': '结构化正文', 'config': {'generate_talking': False, 'script_lang': 'zh', 'result_dir': str(result)}}
            data = web.read_progress(job)
            assert data['phase'] == '生成讲稿' and data['count_done'] == 27 and data['count_total'] == 30
            assert data['phase_expected_s'] is None and data['phase_remaining_s'] == 90 and not data['stalled']
            progress.write_text(json.dumps({'phase': 'script', 'ts': time.time() - 900}) + '\n', encoding='utf-8')
            Path(job_dir, 'run.log').write_text('[course] 讲稿 27/30 已保存\n', encoding='utf-8')
            legacy = web.read_progress(job)
            assert legacy['count_done'] == 27 and not legacy['stalled']
            config = {'slide_lang': 'zh', 'script_lang': 'zh', 'text_model': 'provider/text', 'vision_model': 'provider/vision', 'script_concurrency': 4}
            web._normalize_models(config)
            argv = web._build_argv({**job, 'config': config, 'paper_latex_root': str(tmp)})
            assert argv[argv.index('--model_name_t') + 1] == 'provider/text'
            assert argv[argv.index('--model_name_v') + 1] == 'provider/vision'
            assert argv[argv.index('--script_concurrency') + 1] == '4'
            assert web._artifact_facts({**job, 'config': config})['vlm_model'] == 'provider/vision'
            facts = web._artifact_facts({**job, 'config': config})
            a, b = {**facts, 'model_api_base': 'http://provider-a'}, {**facts, 'model_api_base': 'http://provider-b'}
            assert web.artifact_cache.struct_key(a) != web.artifact_cache.struct_key(b)
            assert web.artifact_cache.deck_key(a) != web.artifact_cache.deck_key(b)
            assert web.artifact_cache.deck_key(a) != web.artifact_cache.deck_key({**a, 'script_generation_version': 1})
            for bad in ({**config, 'script_concurrency': 9}, {**config, 'text_model': 'bad\nname'}):
                try:
                    web._normalize_models(bad)
                    raise AssertionError('非法模型设置未拒绝')
                except web.HTTPException as exc:
                    assert exc.status_code == 422
            import paper_struct
            from wei_utils import get_agent_config
            requests = []
            def complete(**kwargs):
                requests.append(kwargs)
                return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='虚构正文'), finish_reason='stop')], usage=None)
            fake = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=complete)))
            with patch.dict(os.environ, {'OPENAI_API_KEY': 'fake-key', 'PAPER2VIDEO_FORCE_COMPATIBLE': '1'}), patch('openai.OpenAI', return_value=fake):
                paper_struct.call_llm('虚构系统提示', '虚构文本', model='provider/text')
                assert 'max_tokens' not in requests[0], '不能给未知模型强塞 65536 输出上限'
                paper_struct.call_llm('虚构系统提示', '虚构文本', model='provider/text', max_tokens=2048)
                assert requests[1]['max_tokens'] == 2048
                routed = get_agent_config('gpt-4o')
                assert routed['model_platform'].value == 'openai-compatible-model' and not routed['model_config'].get('max_tokens')
            client = TestClient(web.app, client=('127.0.0.1', 50000))
            class Provider(BaseHTTPRequestHandler):
                def log_message(self, *args):
                    pass
                def do_GET(self):
                    if self.path != '/v1/models':
                        self.send_response(404)
                        self.end_headers()
                        return
                    assert self.path == '/v1/models' and self.headers['Authorization'] == 'Bearer fake-key'
                    body = json.dumps({'object': 'list', 'data': [{'id': x, 'object': 'model'} for x in ['provider/vision', 'provider/text', 'provider/text']]}).encode()
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/json')
                    self.end_headers()
                    self.wfile.write(body)
            server = ThreadingHTTPServer(('127.0.0.1', 0), Provider)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                body = {'api_key': 'fake-key', 'api_base': f'http://127.0.0.1:{server.server_port}/v1'}
                response = client.post('/api/models', json=body)
                assert response.status_code == 200 and response.json()['models'] == ['provider/text', 'provider/vision']
                assert client.post('/api/models', json={**body, 'api_base': 'https://example.invalid/?key=secret'}).status_code == 422
                assert client.post('/api/models', json=body, headers={'Origin': 'https://untrusted.invalid'}).status_code == 403
                assert not web._JOB_KEYS, '读取列表不能持久化或记忆 API Key'
                fallback = client.post('/api/models', json={**body, 'api_base': f'http://127.0.0.1:{server.server_port}/missing'})
                assert not fallback.json()['ok'] and '手动输入' in fallback.json()['message']
                web.CONTENT_ROOT = str(tmp / 'contents')
                work = Path(web.CONTENT_ROOT, 'a' * 12, 'ingest')
                work.mkdir(parents=True)
                (work / 'paper_text.md').write_text('这是虚构的本地测试素材。', encoding='utf-8')
                created = client.post('/api/jobs', json={**body, 'ingest_id': 'content_' + 'a' * 12, 'config': config})
                assert created.status_code == 200
                saved_job = web._load_job(created.json()['job_id'])
                assert saved_job['config']['text_model'] == 'provider/text' and saved_job['config']['vision_model'] == 'provider/vision'
                assert 'fake-key' not in Path(web._job_dir(saved_job['id']), 'job.json').read_text(encoding='utf-8')
            finally:
                server.shutdown()
                server.server_close()
                thread.join()
        finally:
            if old_progress is None:
                os.environ.pop('PAPER2VIDEO_PROGRESS_FILE', None)
            else:
                os.environ['PAPER2VIDEO_PROGRESS_FILE'] = old_progress
    print('PASS 有界并发、乱序页序、缓存与失败恢复、单页校验、结构化/旧日志进度、动态剩余时间、模型参数/列表及边界校验')


if __name__ == '__main__':
    main()
