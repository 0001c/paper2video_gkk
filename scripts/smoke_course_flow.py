"""课程/审核回归：隔离数据、替身模型，不调用 GPU 或外部 API。"""
import asyncio
import importlib.util
import json
import os
import tempfile
import sys
import runpy
import types
import subprocess
from pathlib import Path

from fastapi.testclient import TestClient
from PIL import Image
import numpy as np
import soundfile as sf

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('course_smoke_web', ROOT / 'web/app.py')
web = importlib.util.module_from_spec(spec)
spec.loader.exec_module(web)
import course


def main():
    checks = 0
    def check(value, description):
        nonlocal checks
        assert value, description
        checks += 1
        print('PASS', description)

    with tempfile.TemporaryDirectory(prefix='.course-smoke-', dir=ROOT) as work:
        assert os.path.commonpath([str(ROOT), os.path.realpath(work)]) == str(ROOT)
        web.JOBS_ROOT = work
        web.CONTENT_ROOT = os.path.join(work, '_content')
        web.artifact_cache.CACHE_ROOT = os.path.join(work, '_cache')
        web.mimo_voice.CACHE_DIR = os.path.join(work, '_voices')
        web._QUEUE = asyncio.Queue()
        client = TestClient(web.app, client=('127.0.0.1', 50000))
        tex_root = Path(work, 'tex')
        tex_root.mkdir()
        (tex_root / 'main.tex').write_text('\\chapter{第一章}\n\\input{section}\n', encoding='utf-8')
        (tex_root / 'section.tex').write_text('\\section{集合}\n集合定义。', encoding='utf-8')
        expanded = course.latex_source(tex_root)
        check(len(course.source_sections(expanded)) == 3 and '集合定义' in expanded, 'LaTeX 子文件展开后可以按章节选范围')
        for reference in ('../outside', 'main'):
            (tex_root / 'section.tex').write_text('\\input{' + reference + '}', encoding='utf-8')
            try:
                course.latex_source(tex_root)
                raise AssertionError('危险引用未拒绝')
            except ValueError:
                check(True, 'LaTeX 越界和循环引用被拒绝')
        text = '# 第一章\n章节导读\n## 1.1 基本概念\n' + '介绍概念和原理。' * 70 + '\n## 1.2 例题\n' + '源材料例题的步骤。' * 90
        sections = course.source_sections(text)
        parent, child = sections[1], sections[2]
        check(parent['end'] == len(text), '选整章包含其所有小节')
        try:
            course.selected_source(text, [parent['id'], child['id']])
            raise AssertionError('包含关系未拒绝')
        except ValueError:
            check(True, '包含关系与重复范围被拒绝')
        settings = {'section_ids': [s['id'] for s in sections[2:]], 'duration_mode': 'custom',
                    'target_minutes': 40, 'speech_rate': 220, 'audience': '本科学生', 'objectives': '掌握定义与例题', 'focus': '推导', 'depth': '详细解释'}
        plan, source = course.build_plan(text, settings, 12, 'zh')
        check(abs(sum(s['minutes'] for s in plan['sections']) - 40) < 0.02, '自定义时长分配完整且页数连续')
        check(sum(s['last_page'] - s['first_page'] + 1 for s in plan['sections']) == 12, '教案页数不遗漏')
        check('章节导读' not in source, '只选小节不会混入章节外材料')
        for bad in ({**settings, 'target_minutes': float('nan')}, {**settings, 'section_ids': [{}]}, {**settings, 'unknown': True}):
            try:
                course.build_plan(text, bad, 12, 'zh')
                raise AssertionError('非法输入未拒绝')
            except ValueError:
                check(True, '非法数字、范围和未知字段均拒绝')
        content = Path(web.CONTENT_ROOT, 'a' * 12)
        (content / 'ingest').mkdir(parents=True)
        (content / 'ingest/paper_text.md').write_text(text, encoding='utf-8')
        body = {'ingest_id': 'content_' + 'a' * 12, 'settings': settings, 'slide_count': 12, 'script_lang': 'zh'}
        preview = client.post('/api/course/plan', json=body)
        check(preview.status_code == 200 and preview.json()['token'] == plan['token'], '教案预览与创建共用规范及指纹')
        check(client.post('/api/course/plan', json={**body, 'typo': True}).status_code == 422, '规范拒绝拼错字段')
        check(client.post('/api/course/plan', json=body, headers={'Origin': 'https://untrusted.invalid'}).status_code == 403, '跨站写入被拒绝')
        remote = TestClient(web.app, client=('192.0.2.1', 50000))
        check(remote.post('/api/course/plan', json=body).status_code == 503, '非本机写入默认关闭')
        check(client.post('/api/course/plan', content=b'x' * 512001).status_code == 413, '请求体超限在路由前拒绝')
        payload = {'ingest_id': body['ingest_id'], 'config': {'content_profile': 'textbook', 'slide_lang': 'zh', 'script_lang': 'zh', 'slide_count': 12, 'course': settings}, 'course_plan_token': plan['token']}
        bad_job = client.post('/api/jobs', json={**payload, 'course_plan_token': 'outdated'})
        check(bad_job.status_code == 409 and web._QUEUE.empty(), '过期教案不能创建任务')
        created = client.post('/api/jobs', json=payload)
        check(created.status_code == 200, '审核过教案后可以创建准备任务')
        job = web._load_job(created.json()['job_id'])
        check(job['config']['stages'] == ['1', '2'] and job['config']['review_before_render'], '课程初次执行仅准备课件讲稿')
        facts = web._artifact_facts(job)
        check(web.artifact_cache.struct_key(facts) != web.artifact_cache.struct_key({**facts, 'struct_mode': 'paper'}), '相同正文的课程结构化不能误用论文缓存')
        check(Path(work, job['id'], 'ingest/paper_text.md').read_text(encoding='utf-8') == source, '流水线只结构化选定的正文')
        for profile in ('paper', 'story', 'fiction_comic', 'custom'):
            response = client.post('/api/jobs', json={'ingest_id': body['ingest_id'], 'config': {'content_profile': profile, 'slide_lang': 'zh', 'script_lang': 'zh'}})
            check(response.status_code == 200 and web._load_job(response.json()['job_id'])['config']['review_before_render'], profile + ' 也默认暂停审核')
        # 已生成的审核素材，全部位于隔离目录。
        web._QUEUE = asyncio.Queue()
        result = Path(work, job['id'], 'result')
        slides = result / 'slide_imgs'
        slides.mkdir(parents=True)
        for i in range(1, 5):
            Image.new('RGB', (80, 60), (i * 30, 100, 100)).save(slides / (str(i) + '.png'))
        script = result / 'subtitle_w_cursor.txt'
        script.write_text('\n###\n'.join('本页讲解内容。 | no' for _ in range(4)), encoding='utf-8')
        job['status'] = 'awaiting_review'
        job['config'].update(script_concurrency=5, model_api_base='https://provider-one.invalid/v1')
        web._remember_keys(job['id'], {'api_key': 'fake-original-key', 'api_base': 'https://provider-one.invalid/v1'})
        web._save_job(job)
        snapshot = client.get('/api/jobs/' + job['id'] + '/review').json()
        check(snapshot['editable'] and len(snapshot['pages']) == 4, '审核页读取课件和原始讲稿')
        check(len(snapshot['teaching_tasks']) == 4 and all(t['goal'] for t in snapshot['teaching_tasks']), '审核页提供每页讲解任务和后页衔接')
        check(facts['script_generation_version'] == course.SCRIPT_GENERATION_VERSION
              and web.artifact_cache.deck_key(facts) != web.artifact_cache.deck_key({**facts,'script_generation_version':5}),
              '整套课件讲稿缓存也按新教学生成规则隔离')
        edit = {'version': snapshot['version'], 'pages': [*snapshot['pages'][:-1], '已修改的例题解释。 | no']}
        route = '/api/jobs/' + job['id'] + '/review'
        check(client.put(route, json={**edit, 'unknown': 1}).status_code == 422, '讲稿写入拒绝未知字段')
        wrong = client.put(route, json={**edit, 'pages': ['没有机器格式'] * 4})
        check(wrong.status_code == 422 and 'pages' in wrong.text, '错误讲稿返回具体字段且不落盘')
        saved = client.put(route, json=edit)
        check(saved.status_code == 200 and saved.json()['version'] != snapshot['version'], '全量保存讲稿并产生新版本')
        check(len(list((result / 'script_versions').glob('*.txt'))) == 1, '讲稿旧版本被保留')
        check(client.put(route, json=edit).status_code == 409, '过期编辑不能覆盖新版本')
        current = saved.json()
        render = client.post(route + '/run', json={'version': current['version'], 'pages': current['pages'], 'action': 'render', 'api_base': 'https://provider-two.invalid/v1', 'cursor_style': 'arrow-white', 'cursor_scale': 1.5})
        check(render.status_code == 200, '明确审核确认后创建配音合片任务')
        render_job = web._load_job(render.json()['job_id'])
        check(render_job['config']['cursor_style'] == 'arrow-white' and render_job['config']['cursor_scale'] == 1.5 and job['config']['cursor_style'] == 'classic-red', '审核可为成片选择光标，原任务配置保持不变')
        check(render_job['config']['script_concurrency'] == 5, '审核调用未指定并发时保留任务原设置')
        check({name: web._JOB_KEYS[render_job['id']].get(name) for name in ('api_key', 'api_base')} == {'api_key': 'fake-original-key', 'api_base': 'https://provider-two.invalid/v1'}, '续跑只补服务地址也保留任务 Key')
        check(not render_job['config']['review_before_render'] and render_job['config']['require_reuse_script'] and render_job['config']['stages'] == ['2', '3'], '配音只复用已审核讲稿，不再次调用模型')
        check(client.put(route, json={'version': current['version'], 'pages': current['pages']}).status_code == 409, '共享产物被占用时禁止保存')
        render_job['status'] = 'canceled'
        web._save_job(render_job)
        old_prompt_path = job['config']['script_prompt_path']
        old_prompt = Path(old_prompt_path).read_text(encoding='utf-8')
        rewrite = client.post(route + '/run', json={'version': current['version'], 'pages': current['pages'], 'action': 'regenerate', 'page_ids': [1], 'refresh_prompt': True})
        rewrite_job = web._load_job(rewrite.json()['job_id'])
        check(bool(rewrite_job['config']['script_generation_id']) and '--script_generation_id' in web._build_argv(rewrite_job), '每次重写有独立缓存标识并传入流水线')
        check(rewrite_job['config']['script_prompt_path'] != old_prompt_path
              and Path(old_prompt_path).read_text(encoding='utf-8') == old_prompt
              and '正在给学生上这门课' in Path(rewrite_job['config']['script_prompt_path']).read_text(encoding='utf-8'),
              '重写可换用最新角色模板，原任务提示词快照保持不变')
        rewrite_job['status'] = 'failed'
        web._save_job(rewrite_job)
        retry_source = Path(rewrite_job['paper_latex_root'], 'main.tex')
        retry_source.parent.mkdir(parents=True, exist_ok=True)
        retry_source.write_text('测试课程原始工程', encoding='utf-8')
        retry_rewrite = client.post('/api/jobs/' + rewrite_job['id'] + '/prepare-retry', json={'api_key': 'fake-replacement-key'})
        assert retry_rewrite.status_code == 200, retry_rewrite.text
        retry_job = web._load_job(retry_rewrite.json()['job_id'])
        check(retry_job['config']['script_generation_id'] == rewrite_job['config']['script_generation_id'], '失败恢复沿用同一次重写缓存标识')
        check({name: web._JOB_KEYS[retry_job['id']].get(name) for name in ('api_key', 'api_base')} == {'api_key': 'fake-replacement-key', 'api_base': 'https://provider-one.invalid/v1'}, '准备恢复只补 Key 也保留服务地址')
        check(retry_rewrite.status_code == 200 and retry_job['config']['regenerate_pages'] == [1], '失败的局部重写恢复仍只操作原来选定的页')
        retry_job['status'] = 'canceled'
        web._save_job(retry_job)
        legacy = {**rewrite_job, 'id': 'job_legacy_rewrite', 'config': dict(rewrite_job['config'])}
        legacy['config'].pop('script_generation_id')
        Path(web._job_dir(legacy['id'])).mkdir()
        web._save_job(legacy)
        legacy_retry = client.post('/api/jobs/' + legacy['id'] + '/prepare-retry', json={})
        legacy_retry_job = web._load_job(legacy_retry.json()['job_id'])
        check(bool(legacy_retry_job['config'].get('script_generation_id')), '旧局部重写任务首次恢复会补充缓存标识')
        legacy_retry_job['status'] = 'failed'
        web._save_job(legacy_retry_job)
        legacy_again = client.post('/api/jobs/' + legacy_retry_job['id'] + '/prepare-retry', json={})
        legacy_again_job = web._load_job(legacy_again.json()['job_id'])
        check(legacy_again_job['config']['script_generation_id'] == legacy_retry_job['config']['script_generation_id'], '旧任务补充标识后再次恢复可以复用本次成功页')
        legacy_again_job['status'] = 'canceled'
        web._save_job(legacy_again_job)
        check(rewrite.status_code == 200 and rewrite_job['config']['review_before_render'] and rewrite_job['config']['regenerate_pages'] == [1], '局部重写只指定选定页并再次暂停审核')
        rewrite_job['status'] = 'canceled'; web._save_job(rewrite_job)
        # 模型替身验证分批请求、恢复和局部重写。
        calls = []
        def generator(*args, **kwargs):
            calls.append(kwargs)
            return '根据教材解释新的内容。 | no', {'total_tokens': 1}
        output, _ = course.generate_script(plan, slides, job['config']['script_prompt_path'], {'model': 'stub'}, result, generator)
        check(len(calls) == 12 and all(len(c['image_paths']) == 1 for c in calls), '教学讲稿按单页顺序请求并保存，每页最多两次质量修正')
        check(all('教材原文' in c['prompt_text'] and '本页目标约' in c['prompt_text'] and '80~120' not in c['prompt_text'] for c in calls), '长讲稿使用正文和时长预算，移除短讲解限制')
        course.generate_script(plan, slides, job['config']['script_prompt_path'], {'model': 'stub'}, result, generator)
        check(len(calls) == 12, '重新准备复用已经完成的讲稿页')
        script.write_text(output, encoding='utf-8')
        course.generate_script(plan, slides, job['config']['script_prompt_path'], {'model': 'stub'}, result, generator, selected=[1])
        check(len(calls) == 15, '模型只重写选定的一页并有界修正时长')
        import speech_gen as speech
        original = speech.synth_page_qwen3tts
        voice = result / 'ref.wav'
        sf.write(voice, np.zeros(2400, dtype=np.float32), 24000)
        synth_calls = []
        def synth(text, filename, *args, **kwargs):
            synth_calls.append(text)
            sf.write(filename, np.zeros(4800, dtype=np.float32), 24000)
            return filename, 0.2
        speech.synth_page_qwen3tts = synth
        try:
            audio = result / 'audio'
            speech.tts_per_slide('qwen3tts', script, audio, voice, '参考文字', language='Chinese', resume_audio=True)
            first_calls = len(synth_calls)
            speech.tts_per_slide('qwen3tts', script, audio, voice, '参考文字', language='Chinese', resume_audio=True)
            check(len(synth_calls) == first_calls, '未变化的音频逐页复用')
            script.write_text(output.replace('根据教材解释新的内容。', '这一页的讲稿被修改。', 1), encoding='utf-8')
            speech.tts_per_slide('qwen3tts', script, audio, voice, '参考文字', language='Chinese', resume_audio=True)
            check(len(synth_calls) == first_calls + 1, '改一页只重做该页音频')
            sf.write(voice, np.ones(2400, dtype=np.float32) * 0.1, 24000)
            speech.tts_per_slide('qwen3tts', script, audio, voice, '参考文字', language='Chinese', resume_audio=True)
            check(len(synth_calls) == first_calls + 5, '更换参考声音使所有相关音频缓存失效')
            timing = course.export_chapters(plan, result)
            check(timing['actual_minutes'] is not None and (result / 'chapters.json').is_file(), '记录实际时长并输出章节时间表')
        finally:
            speech.synth_page_qwen3tts = original
        import cursor_gen as cursor
        cursor._open_pdf_locator = lambda *args: None
        cursor.release_gui_model = lambda: None
        point_calls, alignment_calls = [], []
        def infer(task):
            point_calls.append(task)
            return {'slide': task[0], 'sentence': task[1], 'speech_text': task[2], 'cursor_prompt': task[3], 'cursor': [20, 20], 'token': ''}
        cursor._infer_task = infer
        speech.align_with_qwen3 = lambda audio_path, text, **kwargs: alignment_calls.append((audio_path,text)) or {'segments':[{'words':[{'word':text,'start':0.02,'end':0.1}]}]}
        cursor.timesteps = lambda *args: [{'start': 0.02, 'end': 0.1, 'text': '测试'}]
        cursor_json = result / 'cursor.json'
        cursor.cursor_gen_per_sentence(script, slides, audio, str(cursor_json), gpu_list='0', language='Chinese', resume_cache=True)
        first_alignments = len(alignment_calls)
        timeline = json.loads(cursor_json.read_text(encoding='utf-8'))
        check(abs(timeline[1]['start'] - 0.22) < 0.01, '页间时间轴包含完整音频时长，停顿不会累计丢失')
        cursor.cursor_gen_per_sentence(script, slides, audio, str(cursor_json), gpu_list='0', language='Chinese', resume_cache=True)
        check(len(alignment_calls) == first_alignments, '未变化页的强制对齐和光标结果复用')
        # 执行真实 pipeline 主入口；替换模型/媒体依赖，确认审核断点在 TTS 之前退出。
        fake_modules = {
            'pdf_render': ['render_pdf_to_images'], 'speech_gen': ['tts_per_slide', 'release_models'],
            'subtitle_render': ['add_subtitles'], 'talking_gen': ['talking_gen_per_slide'],
            'merge_talking': ['merge_slides_and_talking'], 'merge_video': ['merge_slides_and_audio'],
            'talking_config': ['describe_profile', 'resolve_profile'], 'cursor_gen': ['cursor_gen_per_sentence'],
            'slide_code_gen_select_improvement': ['latex_code_gen_upgrade'], 'cursor_render': ['render_video_with_cursor_from_json'],
            'subtitle_cursor_prompt_gen': ['subtitle_cursor_gen', 'validate_script_output'], 'wei_utils': ['get_agent_config'], 'cv2': ['imread']}
        original_modules = {name: sys.modules.get(name) for name in fake_modules}
        original_argv = sys.argv[:]
        def forbidden(*args, **kwargs):
            raise AssertionError('审核前不得调用配音/合片或模型')
        latex = Path(work, 'pipeline_source'); latex.mkdir()
        (latex / 'main.tex').write_text('source fixture', encoding='utf-8')
        try:
            from script_validation import validate_script_output
            for name, functions in fake_modules.items():
                module = types.ModuleType(name)
                for f in functions:
                    setattr(module, f, forbidden)
                sys.modules[name] = module
            sys.modules['subtitle_cursor_prompt_gen'].validate_script_output = validate_script_output
            sys.modules['wei_utils'].get_agent_config = lambda *args: {}
            sys.argv = ['pipeline.py', '--stage', '["2"]', '--paper_latex_root', str(latex),
                        '--result_dir', str(result), '--script_lang', 'zh', '--slide_lang', 'zh',
                        '--script_prompt_file', job['config']['script_prompt_path'], '--reuse_script', 'True',
                        '--require_reuse_script', 'True', '--review_before_render', 'True']
            try:
                runpy.run_path(str(ROOT / 'Paper2Video-main/src/pipeline.py'), run_name='__main__')
                raise AssertionError('审核未暂停')
            except SystemExit as exc:
                check(exc.code == 0 and (result / 'course_review.json').is_file(), '真实流水线在配音和合片前暂停审核')
            timing_plan=Path(work,'timing_plan.json')
            course.write_json(timing_plan,plan)
            sys.argv=['pipeline.py','--stage','["3"]','--paper_latex_root',str(latex),
                      '--result_dir',str(result),'--course_plan',str(timing_plan),'--generate_talking','False']
            try:
                runpy.run_path(str(ROOT/'Paper2Video-main/src/pipeline.py'),run_name='__main__')
                raise AssertionError('不完整或超时音频不应进入合片')
            except ValueError as exc:
                check('音频时长未通过合片前验收' in str(exc),'直接续跑合片也不能绕过真实音频时长验收')
        finally:
            sys.argv = original_argv
            for name, module in original_modules.items():
                if module is None:
                    sys.modules.pop(name, None)
                else:
                    sys.modules[name] = module
        job.update(status='failed', paper_latex_root=str(latex))
        web._save_job(job)
        retry = client.post('/api/jobs/' + job['id'] + '/prepare-retry', json={})
        check(retry.status_code == 200 and web._load_job(retry.json()['job_id'])['config']['review_before_render'], '准备阶段失败可以恢复，仍须审核确认')
        if '--media' in sys.argv:
            subprocess.run(['ffmpeg', '-y', '-v', 'error', '-f', 'lavfi', '-i', 'color=c=white:s=64x64:r=10:d=0.8',
                            '-c:v', 'libx264', '-pix_fmt', 'yuv420p', str(result / '3_merage.mp4')], check=True)
            course.embed_chapters(plan, result)
            probe = json.loads(subprocess.check_output(['ffprobe', '-v', 'error', '-show_chapters', '-of', 'json', str(result / 'course_video.mp4')]))
            check(len(probe['chapters']) == len(plan['sections']), 'MP4 章节实际封装通过 ffprobe 验证')
    print('全部通过：{} 项；仅使用替身模型和隔离临时目录'.format(checks))


if __name__ == '__main__':
    main()
