"""可选评价、定向修复、不可变成片备份的离线验收；零收费模型调用。"""
import asyncio
import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch
import wave

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'Paper2Video-main/src')]
from pydantic import ValidationError
from fastapi.testclient import TestClient
from PIL import Image
from web import app as web, video_versions
from web.model_config import EvaluationPolicy, PipelineModelConfig, legacy_config, evaluation_roles, required_roles
import quality_evaluation as quality
import qa_evaluation as qa
import bailian_speech
import speech_gen
from scripts import check_qa_evaluation as qa_fixture
from scripts import check_bailian_speech as speech_fixture


def policy(*names):
    return EvaluationPolicy(enabled=True, question_count=4, checks={name: {'enabled': name in names, 'threshold': 80}
        for name in ('script', 'video', 'slides', 'presentation', 'audio')})


class PolicyChecks(unittest.TestCase):
    def test_selection_legacy_and_empty_or_invalid_threshold(self):
        self.assertEqual(set(EvaluationPolicy(enabled=True).selected()), {'script', 'video'})
        self.assertFalse(EvaluationPolicy().selected())
        self.assertEqual(set(policy('slides').selected('video')), {'slides'})
        for value in ({'enabled': True, 'checks': {}}, {'checks': {'slides': {'threshold': 101}}}, {'checks': {'audio': {'threshold': True}}}):
            with self.assertRaises(ValidationError):
                EvaluationPolicy.model_validate(value)

    def test_visual_only_does_not_require_question_generator_or_asr(self):
        config = legacy_config({}, 'fixture').public_dict()
        config['stages']['qa'] = copy.deepcopy(config['stages']['vision'])
        config['stages']['asr'] = {'mode': 'api', 'provider': 'bailian', 'model': 'fun-asr', 'connection_id': 'fixture'}
        config['evaluation'] = policy('slides').model_dump()
        checked = PipelineModelConfig.model_validate(config)
        self.assertEqual(evaluation_roles(checked, 'video'), ['qa'])
        self.assertEqual(evaluation_roles(checked, 'script'), [])
        self.assertEqual(required_roles(checked, {'stages': ['1', '2'], 'review_before_render': True}), ['text', 'vision'])
        config['evaluation'] = policy('audio').model_dump()
        with self.assertRaises(ValidationError):
            PipelineModelConfig.model_validate(config)

    def test_route_requires_all_selected_checks_and_never_auto_rewrites_visual_audio(self):
        selected = policy('video', 'slides', 'audio')
        report = {'stage': 'video', 'checks': {'video': {'score': 100}, 'slides': {'score': 70}, 'audio': {'score': 100}}}
        self.assertEqual(quality.report_route(report, selected), 'review')
        report['checks']['slides']['score'] = 100
        report['checks']['audio']['score'] = 40
        self.assertEqual(quality.report_route(report, selected), 'review')
        report['checks']['audio']['score'] = 100
        report['checks']['video']['score'] = 40
        self.assertEqual(quality.report_route(report, selected), 'retry')
        report['checks']['video']['issues'] = {'wrong_answer': 0, 'unanswered': 0, 'evidence_unmatched': 4}
        self.assertEqual(quality.report_route(report, selected), 'review', '纯证据格式失败不能触发收费重写')
        report['checks']['video']['issues']['wrong_answer'] = 1
        self.assertEqual(quality.report_route(report, selected), 'retry')
        self.assertEqual(quality.report_route(report, selected, 2), 'review')
        report['checks']['video']['score'] = 80
        self.assertEqual(quality.report_route(report, selected), 'pass')
        del report['checks']['audio']
        self.assertEqual(quality.report_route(report, selected), 'review')


class QualityChecks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=ROOT / 'jobs/_validation', prefix='quality-evidence-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'slide_imgs').mkdir()
        (self.root / 'audio').mkdir()
        (self.root / 'merage').mkdir()
        for index in range(2):
            Image.new('RGB', (640, 360), ('white', 'blue')[index]).save(self.root / f'slide_imgs/{index + 1}.png')
            with wave.open(str(self.root / f'audio/{index}.wav'), 'wb') as wav:
                wav.setnchannels(1); wav.setsampwidth(2); wav.setframerate(16000); wav.writeframes(bytes(32000))
        self.video = self.root / '3_merage.mp4'
        subprocess.run(['ffmpeg', '-v', 'error', '-y', '-f', 'lavfi', '-i', 'color=c=white:s=640x360:r=30',
            '-f', 'lavfi', '-i', 'sine=frequency=400:sample_rate=16000', '-t', '2', '-c:v', 'libx264', '-c:a', 'aac', str(self.video)], check=True, capture_output=True)
        qa.write_json(self.root / 'merage/media_timeline.json', {'pages': [{'page': i + 1, 'audio_start_seconds': i, 'audio_end_seconds': i + 1} for i in range(2)]})
        (self.root / 'subtitle_w_cursor.txt').write_text('原文知识 | no\n###\n原文知识 | no', encoding='utf-8')
        self.config = legacy_config({}, 'fixture').public_dict()
        self.config['stages']['qa'] = copy.deepcopy(self.config['stages']['vision'])

    def test_actual_video_per_page_frames_and_audio_cache_not_synthetic_subtitles(self):
        transcribe = MagicMock(return_value='实际音轨识别')
        result = quality.page_evidence(self.video, self.root, policy('slides', 'presentation', 'audio'), 'zh', transcribe)
        self.assertEqual(len(result), 2)
        self.assertEqual([len(row['frames']) for row in result], [3, 3])
        self.assertEqual(transcribe.call_count, 2)
        self.assertTrue(all('实际音轨识别' == row['transcript'][0]['text'] for row in result))
        quality.page_evidence(self.video, self.root, policy('slides', 'presentation', 'audio'), 'zh', transcribe)
        self.assertEqual(transcribe.call_count, 2)
        self.assertTrue(all(Path(frame['path']).is_file() for row in result for frame in row['frames']))
        one_frame = quality.page_evidence(self.video, self.root, policy('slides'), 'zh', transcribe)
        self.assertTrue(all(len(row['frames']) == 1 for row in one_frame))
        three_frames = quality.page_evidence(self.video, self.root, policy('presentation'), 'zh', transcribe)
        self.assertTrue(all(len(row['frames']) == 3 for row in three_frames))
        video_audio = quality.page_evidence(self.video, self.root, policy('video', 'audio'), 'zh', transcribe)
        self.assertTrue(all(len(row['frames']) == 1 for row in video_audio))
        timeline = qa.read_json(self.root / 'merage/media_timeline.json')
        timeline['pages'][1]['audio_end_seconds'] = 5
        qa.write_json(self.root / 'merage/media_timeline.json', timeline)
        with self.assertRaisesRegex(ValueError, '时长不符'):
            quality.page_evidence(self.video, self.root, policy('slides'), 'zh', transcribe)

    def test_visual_only_no_source_questions_or_asr_and_minimum_page_score(self):
        selected = policy('slides')
        self.config['evaluation'] = selected.model_dump()
        config = PipelineModelConfig.model_validate(self.config)
        calls = []
        def request(settings, content, root):
            data = json.loads(content[0]['text'].split('\n')[-1])
            calls.append(data)
            score = 100 if data['page'] == 1 else 70
            timestamp = float(content[1]['text'].split()[-2])
            return {'checks': {'slides': {'criteria': {name: score for name in quality.CRITERIA['slides']},
                'issues': [] if score == 100 else [{'criterion': 'readability', 'description': '文字较小', 'evidence': '右下角公式难以辨认', 'timestamp': timestamp}]}}}
        output = self.root / 'quality.json'
        with patch.object(quality, 'role_settings', return_value={'model_type': 'fixture'}), patch.object(quality, 'question_bank', side_effect=AssertionError('不得出题')):
            report = quality.evaluate_checks({}, config, '', self.root, output, 'video', 'zh', request)
            self.assertEqual(report['checks']['slides']['score'], 70)
            self.assertEqual(report['checks']['slides']['mean_score'], 85)
            self.assertFalse(report['passed'])
            self.assertEqual(report['total'], 0)
            self.assertEqual(len(calls), 2)
            quality.evaluate_checks({}, config, '', self.root, output, 'video', 'zh', request)
            self.assertEqual(len(calls), 2)
            self.config['evaluation']['checks']['slides']['threshold'] = 65
            next_report = quality.evaluate_checks({}, PipelineModelConfig.model_validate(self.config), '', self.root, output, 'video', 'zh', request)
            self.assertTrue(next_report['passed'])
            self.assertNotEqual(report['fingerprint'], next_report['fingerprint'])

    def test_rubric_rejects_unlocated_visual_or_fabricated_audio_quotes(self):
        page = {'page': 1, 'start': 0, 'end': 1, 'frames': [{'timestamp': 0.5}]}
        result = {'criteria': {name: 70 for name in quality.CRITERIA['slides']}, 'issues': [{'criterion': 'layout', 'description': '问题', 'evidence': '画面证据', 'timestamp': 9}]}
        with self.assertRaisesRegex(ValueError, '抽帧时间'):
            quality.rubric_result('slides', result, page, '', '')
        result = {'criteria': {name: 70 for name in quality.CRITERIA['audio']}, 'issues': [{'criterion': 'accuracy', 'description': '问题', 'evidence': '差异', 'script_quote': '捏造', 'asr_quote': ''}]}
        with self.assertRaisesRegex(ValueError, '引文'):
            quality.rubric_result('audio', result, page, '实际讲稿', '实际音轨')
        result['issues'][0]['script_quote'] = '实际讲稿'
        self.assertTrue(quality.rubric_result('audio', result, page, '实际讲稿', '')['issues'][0]['requires_review'])
        result['issues'] = []
        with self.assertRaisesRegex(ValueError, '缺少具体问题'):
            quality.rubric_result('audio', result, page, '', '')

    def test_failed_rubric_preserves_diagnostic_and_reuses_valid_pages(self):
        self.config['evaluation'] = policy('slides').model_dump()
        config = PipelineModelConfig.model_validate(self.config)
        calls = []
        failing = True
        def request(settings, content, root):
            # 本测试使用无问题模板，不依赖纠正提示里的上一轮JSON。
            page = 2 if '"page": 2' in content[0]['text'] else 1
            calls.append(page)
            criteria = {name: 100 for name in quality.CRITERIA['slides']}
            if page == 2 and failing:
                criteria.pop('visual_teaching')
            return {'checks': {'slides': {'criteria': criteria, 'issues': []}}}
        with patch.object(quality, 'role_settings', return_value={'model_type': 'fixture'}):
            with self.assertRaisesRegex(ValueError, '规定维度'):
                quality.evaluate_checks({}, config, '', self.root, self.root / 'quality.json', 'video', 'zh', request)
            self.assertEqual(len(list((self.root / 'evaluation').glob('rubric-*-page-1.json'))), 1)
            self.assertEqual(len(list((self.root / 'evaluation').glob('rubric-*-page-2.response.json'))), 1)
            self.assertFalse((self.root / 'quality.json').is_file(), '失败不能发布完整评价')
            self.assertEqual(calls, [1, 2, 2, 2], '失败页只尝试三次，成功页不重复调用')
            failing = False
            report = quality.evaluate_checks({}, config, '', self.root, self.root / 'quality.json', 'video', 'zh', request)
            self.assertEqual(calls, [1, 2, 2, 2, 2])
            self.assertTrue(report['passed'])

    def test_visual_timestamp_accepts_exact_decimal_string_and_rejects_fabrication(self):
        page = {'page': 1, 'start': 0, 'end': 37.04, 'frames': [{'timestamp': 2.963}, {'timestamp': 18.52}, {'timestamp': 34.077}]}
        result = {'criteria': {name: 90 for name in quality.CRITERIA['slides']},
                  'issues': [{'criterion': 'layout', 'description': '正文较密', 'evidence': '右下角文字较密', 'timestamp': '2.963'}]}
        row = quality.rubric_result('slides', result, page, '', '')
        self.assertEqual(row['issues'][0]['timestamp'], 2.963)
        self.assertEqual(row['score'], 90, '表示转换不改模型评分')
        for invalid in ('9.963', '2.963秒', '', 'NaN', 'Infinity', '2.963;print(1)', None, True, float('nan'), float('inf')):
            result['issues'][0]['timestamp'] = invalid
            with self.subTest(invalid=repr(invalid)), self.assertRaisesRegex(ValueError, '抽帧时间'):
                quality.rubric_result('slides', result, page, '', '')

    def test_bad_frame_timestamp_recovers_automatically_without_relaxing_validation(self):
        self.config['evaluation'] = policy('slides').model_dump()
        config = PipelineModelConfig.model_validate(self.config)
        calls = []
        def request(settings, content, root):
            page = 2 if '"page": 2' in content[0]['text'] else 1
            calls.append(page)
            actual = float(content[1]['text'].split()[-2])
            if len(calls) == 2:
                self.assertIn('实际抽帧秒数', content[0]['text'])
                self.assertIn('视觉问题未引用', content[0]['text'])
            return {'checks': {'slides': {'criteria': {name: 90 for name in quality.CRITERIA['slides']},
                'issues': [{'criterion': 'layout', 'description': '版面较密', 'evidence': '右下角文字较密',
                            'timestamp': actual + 100 if len(calls) == 1 else actual}]}}}
        with patch.object(quality, 'role_settings', return_value={'model_type': 'fixture'}):
            report = quality.evaluate_checks({}, config, '', self.root, self.root / 'report.json', 'video', 'zh', request)
        self.assertEqual(calls, [1, 1, 2])
        self.assertTrue(report['passed'])
        self.assertEqual(len(report['checks']['slides']['pages']), 2, '失败尝试不能增加重复页分')
        self.assertLess(report['checks']['slides']['issues'][0]['timestamp'], 2)

    def test_transient_request_retries_but_auth_and_token_limits_do_not(self):
        page = {'page': 1, 'start': 0, 'end': 1, 'frames': []}
        good = {'checks': {'slides': {'criteria': {name: 100 for name in quality.CRITERIA['slides']}, 'issues': []}}}
        class HttpError(Exception):
            def __init__(self, status):
                self.status_code = status
        with patch.object(quality.time, 'sleep'):
            request = MagicMock(side_effect=[HttpError(503), good])
            quality.rubric_page({}, [{'type': 'text', 'text': '材料'}], self.root, self.root / 'temporary.json',
                                ['slides'], page, '', '', request)
            self.assertEqual(request.call_count, 2)
            for failure in (HttpError(401), ValueError('评价模型输出达到token上限而截断')):
                request = MagicMock(side_effect=failure)
                with self.assertRaises(type(failure)):
                    quality.rubric_page({}, [{'type': 'text', 'text': '材料'}], self.root, self.root / 'missing.json',
                                        ['slides'], page, '', '', request)
                self.assertEqual(request.call_count, 1)

    def test_evidence_format_repair_keeps_answers_and_never_receives_correct_keys(self):
        self.config['stages']['quiz'] = {**self.config['stages']['text'], 'model': 'fixture-quiz'}
        self.config['evaluation'] = policy('script').model_dump()
        config = PipelineModelConfig.model_validate(self.config)
        source = self.root / 'source.md'
        source.write_text('原文知识', encoding='utf-8')
        bank = {'id': 'fixture', 'questions': qa_fixture.questions()}
        calls = []
        def request(settings, content, root):
            calls.append(content)
            if len(calls) == 2:
                self.assertIn('保持', content[0]['text'])
                self.assertNotIn('source_quote', content[0]['text'])
                self.assertNotIn('correct_content', content[0]['text'])
            return {'answers': [{'id': str(i + 1), 'answer': 'A',
                'evidence_quotes': ['捏造原句' if len(calls) == 1 else '原文知识']} for i in range(4)]}
        with patch.object(quality, 'role_settings', return_value={'model_type': 'fixture'}), patch.object(quality, 'question_bank', return_value=bank):
            report = quality.evaluate_checks({}, config, source, self.root, self.root / 'report.json', 'script', 'zh', request)
        self.assertEqual(len(calls), 2)
        self.assertEqual(report['score'], 100)
        self.assertTrue(report['checks']['script']['evidence_format_repair'])
        self.assertTrue(all(row['verified_evidence'] == ['原文知识'] for row in report['items']))

    def test_evidence_format_repair_cannot_change_answer_to_earn_points(self):
        self.config['stages']['quiz'] = {**self.config['stages']['text'], 'model': 'fixture-quiz'}
        self.config['evaluation'] = policy('script').model_dump()
        config = PipelineModelConfig.model_validate(self.config)
        source = self.root / 'source.md'
        source.write_text('原文知识', encoding='utf-8')
        calls = []
        def request(settings, content, root):
            calls.append(content)
            return {'answers': [{'id': str(i + 1), 'answer': 'B' if len(calls) == 1 else 'A',
                'evidence_quotes': ['捏造原句' if len(calls) == 1 else '原文知识']} for i in range(4)]}
        # 错误选项仍有待修正的引文：不得借格式修正改选正确答案。
        first = request
        def both(settings, content, root):
            response = first(settings, content, root)
            response['answers'][0]['answer'] = 'A' if len(calls) == 1 else 'B'
            return response
        with patch.object(quality, 'role_settings', return_value={'model_type': 'fixture'}), patch.object(quality, 'question_bank', return_value={'id': 'fixture', 'questions': qa_fixture.questions()}):
            with self.assertRaisesRegex(ValueError, '改变了答题选项'):
                quality.evaluate_checks({}, config, source, self.root, self.root / 'report.json', 'script', 'zh', both)
        self.assertFalse((self.root / 'report.json').is_file())

    def test_selected_audio_and_presentation_use_actual_audio_and_do_not_generate_quiz(self):
        self.config['evaluation'] = policy('audio', 'presentation').model_dump()
        config = PipelineModelConfig.model_validate(self.config)
        real_evidence = quality.page_evidence
        transcribe = MagicMock(return_value='原文知识')
        calls = []
        def request(settings, content, root):
            data = json.loads(content[0]['text'].split('\n')[-1])
            calls.append(data)
            self.assertEqual(set(data['checks']), {'audio', 'presentation'})
            self.assertEqual(data['actual_audio_asr'], '原文知识')
            return {'checks': {name: {'criteria': {criterion: 100 for criterion in quality.CRITERIA[name]}, 'issues': []} for name in data['checks']}}
        with (patch.object(quality, 'role_settings', return_value={'model_type': 'fixture'}),
              patch.object(quality, 'question_bank', side_effect=AssertionError('不得出题')),
              patch.object(quality, 'page_evidence', side_effect=lambda video, root, selected, language: real_evidence(video, root, selected, language, transcribe))):
            report = quality.evaluate_checks({}, config, '', self.root, self.root / 'report.json', 'video', 'zh', request)
        self.assertTrue(report['passed'])
        self.assertEqual(set(report['checks']), {'audio', 'presentation'})
        self.assertEqual(len(calls), 2)
        self.assertEqual(transcribe.call_count, 2)


class RepairChecks(unittest.TestCase):
    def setUp(self):
        qa_fixture.FlowChecks.setUp(self)
        local_write = patch.object(web, '_local_write', return_value=None)
        local_write.start()
        self.addCleanup(local_write.stop)
        self.config['evaluation'] = policy('slides', 'presentation', 'audio').model_dump()
        self.job['config'].update(model_config=self.config, review_before_render=False)
        project = self.root / 'original/latex_proj'
        project.mkdir()
        (project / 'main.tex').write_text('原文工程', encoding='utf-8')
        self.job['paper_latex_root'] = str(project)
        self.report = {'stage': 'video', 'language': 'zh', 'fingerprint': 'a' * 64, 'score': 60, 'evaluation_version': quality.VERSION,
            'model_selection': self.config['stages']['qa'], 'inputs': quality.material_inputs(self.result, policy('slides', 'presentation', 'audio'), 'video'),
            'material_sha256': qa.digest_file(self.result / '3_merage.mp4'), 'check_selection': quality.selection(policy('slides', 'presentation', 'audio'), 'video'),
            'checks': {name: {'score': 60, 'threshold': 80, 'issues': [{'page': 1, 'description': '问题'}],
                       'pages': [{'page': 1, 'score': 60, 'issues': [{'description': '问题'}]}]} for name in ('slides', 'presentation', 'audio')}}
        self.job['evaluation'] = {'video': self.report}
        web._save_job(self.job)

    def test_policy_or_material_changes_reject_current_report(self):
        self.assertTrue(web._qa_report_current(self.job, self.report, 'video'))
        changed = copy.deepcopy(self.job)
        changed['config']['model_config']['evaluation']['checks']['slides']['threshold'] = 50
        self.assertFalse(web._qa_report_current(changed, self.report, 'video'))
        (self.result / 'subtitle_w_cursor.txt').write_text('内容变更 | no', encoding='utf-8')
        self.assertFalse(web._qa_report_current(self.job, self.report, 'video'))

    def test_repair_routes_isolate_outputs_and_preserve_backup_bytes_and_reports(self):
        original = (self.result / '3_merage.mp4').read_bytes()
        for action, stages in [('repair_presentation', ['3']), ('repair_audio', ['2', '3']), ('repair_slides', ['1', '2'])]:
            with self.subTest(action=action):
                job = copy.deepcopy(self.job)
                web._save_job(job)
                response = asyncio.run(web.api_evaluation_decision('original', web.EvaluationDecisionRequest(action=action, fingerprint='a' * 64), None))
                child = web._load_job(response['job_id'])
                self.assertEqual(child['config']['stages'], stages)
                self.assertNotEqual(child['config']['result_dir'], str(self.result))
                self.assertFalse(Path(child['config']['result_dir'], '3_merage.mp4').exists())
                self.assertEqual(child['video_versions_owner'], 'original')
                versions = video_versions.list_versions(child, self.root)
                self.assertTrue(versions)
                self.assertEqual(video_versions.version_file(child, self.root, versions[0]['id'], 'video').read_bytes(), original)
                manifest = json.loads(video_versions.version_file(child, self.root, versions[0]['id'], 'report').read_text(encoding='utf-8'))
                self.assertEqual(manifest['evaluation'], job['evaluation'])
                if action == 'repair_audio':
                    self.assertEqual(child['config']['force_audio_pages'], [0])
                if action == 'repair_slides':
                    self.assertNotEqual(child['paper_latex_root'], job['paper_latex_root'])
                    self.assertIn('QUALITY_FEEDBACK', Path(child['config']['slide_prompt_path']).read_text(encoding='utf-8'))
                self.assertEqual((self.result / '3_merage.mp4').read_bytes(), original)
                with self.assertRaises(web.HTTPException):
                    asyncio.run(web.api_evaluation_decision('original', web.EvaluationDecisionRequest(action=action, fingerprint='a' * 64), None))

    def test_backup_failure_stops_queue_and_review_overwrite(self):
        before = (self.result / 'subtitle_w_cursor.txt').read_bytes()
        with patch.object(video_versions.shutil, 'copy2', side_effect=OSError('模拟磁盘写入失败')):
            with self.assertRaises(web.HTTPException):
                asyncio.run(web.api_evaluation_decision('original', web.EvaluationDecisionRequest(action='repair_audio', fingerprint='a' * 64), None))
            with self.assertRaises(web.HTTPException):
                web._save_review(self.job, '新讲稿 | no')
        self.assertTrue(web._QUEUE.empty())
        self.assertEqual((self.result / 'subtitle_w_cursor.txt').read_bytes(), before)
        self.assertEqual(video_versions.list_versions(self.job, self.root), [])

    def test_immutable_multiple_versions_and_range_download(self):
        first = video_versions.archive(self.job, self.root, self.result, '修复前')
        self.assertEqual(video_versions.archive(self.job, self.root, self.result, '重复检查'), first)
        (self.result / '3_merage.mp4').write_bytes(b'new repaired video')
        second = video_versions.archive(self.job, self.root, self.result, '修复完成')
        self.assertNotEqual(first, second)
        self.assertEqual(video_versions.version_file(self.job, self.root, first, 'video').read_bytes(), b'original video')
        client = TestClient(web.app)
        response = client.get(f'/api/jobs/original/video-versions/{first}/video', headers={'Range': 'bytes=0-3'})
        self.assertEqual(response.status_code, 206)
        self.assertEqual(response.content, b'orig')
        self.assertEqual(len(client.get('/api/jobs/original/video-versions').json()['items']), 2)
        self.assertEqual(client.get('/api/jobs/original/video-versions/bad/video').status_code, 404)

    def test_no_script_check_skips_evaluator_without_fabricated_report(self):
        with patch.object(web, '_review_snapshot', return_value={}), patch.object(web, '_qa_start_next', new=MagicMock(side_effect=lambda *args, **kwargs: asyncio.sleep(0))) as next_task:
            asyncio.run(web._qa_evaluate_and_route(self.job, 'script'))
        next_task.assert_called_once()
        self.assertNotIn('script', web._load_job('original')['evaluation'])

    def test_unselected_stage_rejected_and_audio_repair_resume_preserves_generation(self):
        with self.assertRaises(web.HTTPException) as caught:
            asyncio.run(web.api_evaluation_run('original', web.EvaluationRunRequest(stage='script'), None))
        self.assertEqual(caught.exception.status_code, 422)
        self.assertTrue(web._QUEUE.empty())
        response = asyncio.run(web.api_evaluation_decision('original', web.EvaluationDecisionRequest(action='repair_audio', fingerprint='a' * 64), None))
        child = web._load_job(response['job_id'])
        child.update(status='failed')
        web._save_job(child)
        resumed = asyncio.run(web._create_rerun(child['id'], {'reuse_script': True, 'qa_override': True}))
        next_job = web._load_job(resumed['job_id'])
        self.assertEqual(next_job['config']['audio_generation_id'], child['config']['audio_generation_id'])
        self.assertEqual(next_job['config']['force_audio_pages'], [0])

    def test_continue_uses_current_policy_and_subtitle_settings_and_rejects_duplicate(self):
        payload = web.EvaluationDecisionRequest(action='continue', model_config=self.config, subtitle_style={'margin_bottom': 12})
        with patch.object(web, '_review_snapshot', return_value={}):
            response = asyncio.run(web.api_evaluation_decision('original', payload, None))
            child = web._load_job(response['job_id'])
            self.assertEqual(child['config']['subtitle_style']['margin_bottom'], 12)
            self.assertEqual(child['config']['model_config']['evaluation']['checks'], self.config['evaluation']['checks'])
            with self.assertRaises(web.HTTPException):
                asyncio.run(web.api_evaluation_decision('original', payload, None))


class AudioRepairChecks(unittest.TestCase):
    def test_local_forces_target_page_bypasses_old_chunks_and_resumes_completed_repair(self):
        calls = []
        def synthesize(text, output, *args, **kwargs):
            calls.append(text)
            with wave.open(str(output), 'wb') as wav:
                wav.setnchannels(1); wav.setsampwidth(2); wav.setframerate(16000)
                wav.writeframes(speech_fixture.PCM)
        with tempfile.TemporaryDirectory(dir=ROOT / 'jobs/_validation') as directory:
            root = Path(directory)
            script, reference, audio = root / 'script.txt', root / 'reference.wav', root / 'audio'
            script.write_text('甲 | no\n###\n乙 | no', encoding='utf-8')
            synthesize('参考', reference)
            calls.clear()
            with patch.object(speech_gen, 'synth_page_qwen3tts', side_effect=synthesize):
                speech_gen.tts_per_slide('qwen3tts', script, str(audio), reference, ref_text='参考', language='Chinese')
                old_second = (audio / '1.wav').read_bytes()
                for _ in range(2):
                    speech_gen.tts_per_slide('qwen3tts', script, str(audio), reference, ref_text='参考', language='Chinese', force_pages=[0], generation_id='b' * 32)
            self.assertEqual(calls, ['甲', '乙', '甲'])
            self.assertEqual((audio / '1.wav').read_bytes(), old_second)

    def test_cloud_forces_only_selected_page_and_resume_does_not_pay_twice(self):
        calls = []
        class FakeSpeech:
            def synthesize(self, model, voice, text, rate, language):
                calls.append(text)
                return speech_fixture.PCM, {'segments': [{'words': [{'word': text, 'start': 0, 'end': 1}]}]}
        snapshot = speech_fixture.SpeechTests().snapshot()
        parsed = [[('甲', 'no')], [('乙', 'no')]]
        with tempfile.TemporaryDirectory(dir=ROOT / 'jobs/_validation') as directory:
            bailian_speech.tts_pages(parsed, directory, snapshot, client=FakeSpeech())
            old_second = Path(directory, '1.wav').read_bytes()
            for _ in range(2):
                bailian_speech.tts_pages(parsed, directory, snapshot, client=FakeSpeech(), force_pages=[0], generation_id='a' * 32)
            self.assertEqual(calls, ['甲', '乙', '甲'])
            self.assertEqual(Path(directory, '1.wav').read_bytes(), old_second)
            with self.assertRaisesRegex(ValueError, '有效页索引'):
                bailian_speech.tts_pages(parsed, directory, snapshot, client=FakeSpeech(), force_pages=[8], generation_id='a' * 32)


if __name__ == '__main__':
    unittest.main()
