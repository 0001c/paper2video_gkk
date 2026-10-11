"""问答门禁、自动分流和历史产物保护的离线回归；零HTTP、零权重加载。"""
import asyncio
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from pydantic import ValidationError

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'Paper2Video-main/src')]
import qa_evaluation as qa
from web import app as web
from web.model_config import EvaluationPolicy, ConfigurationError, PipelineModelConfig, legacy_config, required_roles
from web.model_connections import ConnectionCreate, ConnectionStore

REAL_QA_CONFIGURE = web._configure_qa_models


def questions(count=4):
    return [{'id': str(i + 1), 'question': f'问题{i + 1}', 'options': dict(A='原文知识', B='乙', C='丙', D='丁'),
             'answer': 'A', 'kind': 'detail' if i % 2 else 'understanding', 'source_quote': '原文知识'} for i in range(count)]


class ScoringChecks(unittest.TestCase):
    def test_quote_layout_differences_restore_exact_source_span(self):
        source = '前文。本研究借鉴 Zhang 等的研究，从预测性、自省性、包容性和响应性四个方面测量科研人员责任式\n\n创新行为[40]。'
        quote = '本研究借鉴Zhang等的研究，从预测性、自省性、包容性和响应性四个方面测量科研人员责任式创新行为'
        restored = qa.verified_quote(quote, source)
        self.assertIn(restored, source)
        self.assertIn('责任式\n\n创新行为', restored)
        rows = questions(1)
        rows[0]['source_quote'] = quote
        normalized = qa.validate_questions(rows, source, 1)
        self.assertEqual(normalized[0]['source_quote'], restored)
        self.assertIsNone(qa.verified_quote(quote.replace('四个方面', '五个方面'), source))
        self.assertIsNone(qa.verified_quote('前文。……责任式创新行为', source))
        self.assertIsNone(qa.verified_quote(' \n\t ', source))
        self.assertIsNone(qa.verified_quote('样本值为12', '样本值为1 2'))
        self.assertIsNone(qa.verified_quote('responsibleinnovation', 'responsible innovation'))
        self.assertEqual(qa.verified_quote('responsible innovation', 'responsible\ninnovation'), 'responsible\ninnovation')

    def test_option_variants_preserve_labels_and_answers(self):
        expected = dict(A='原文知识', B='乙', C='丙', D='丁')
        variants = [
            ['原文知识', '乙', '丙', '丁'],
            ['D. 丁', 'Ａ：原文知识', '(C) 丙', 'b、乙'],
            {'ｄ': '丁', 'Ａ': '原文知识', 'b.': '乙', '(C)': '丙'},
            [{'label': label, 'text': expected[label]} for label in 'DCBA'],
        ]
        for variant in variants:
            with self.subTest(variant=variant):
                rows = questions(1)
                rows[0].update(options=variant, answer=' ａ． ')
                normalized = qa.validate_questions(rows, '原文知识', 1)
                self.assertEqual(normalized[0]['options'], expected)
                self.assertEqual(normalized[0]['answer'], 'A')
                self.assertEqual(normalized[0]['source_quote'], '原文知识')

    def test_invalid_options_and_ambiguous_answers_remain_blocked(self):
        invalid = [None, 'A.原文知识 B.乙 C.丙 D.丁', ['原文知识', '乙', '丙'],
            {'A': '原文知识', 'B': '乙', 'C': '丙'},
            {'A': '原文知识', 'B': '乙', 'C': '丙', 'D': ''},
            ['A.原文知识', 'A.乙', 'C.丙', 'D.丁'],
            ['A.', 'B.乙', 'C.丙', 'D.丁'],
            ['A.原文知识', '乙', '丙', '丁'],
            ['原文知识', '乙', '丙', '丙'],
            [{'label': 'A', 'text': '原文知识', 'explanation': '不能擅自消除字段歧义'}] * 4,
        ]
        for variant in invalid:
            with self.subTest(variant=variant):
                rows = questions(1)
                rows[0]['options'] = variant
                with self.assertRaisesRegex(ValueError, '第1题的options'):
                    qa.validate_questions(rows, '原文知识', 1)
        rows = questions(1)
        rows[0]['answer'] = 'A或B'
        with self.assertRaises(ValueError):
            qa.validate_questions(rows, '原文知识', 1)

    def test_activation_error_has_chinese_actionable_message(self):
        details = {'http_status': 400, 'code': 'invalid_parameter_error',
            'provider_message': 'The product is not activated, please confirm that you have activated products and try again after activation.'}
        message = qa.error_message(details)
        self.assertIn('产品尚未开通', message)
        self.assertIn('已可调用的问答模型', message)
        self.assertIn('HTTP 400', message)
        self.assertNotIn('未填写', message)
        self.assertTrue(details['provider_message'].startswith('The product'))

    def test_provider_failure_details_redact_credentials(self):
        import httpx
        from openai import BadRequestError
        response = httpx.Response(400, request=httpx.Request('POST', 'http://localhost/v1/chat/completions'))
        exc = BadRequestError('必须避免记录的异常原文', response=response, body={'error': {
            'code': 'InvalidParameter', 'param': 'model', 'message': 'API_KEY=secret-test; Bearer secret-test; sk-test-secret 无效型号'}})
        with patch.dict(os.environ, {'PAPER2VIDEO_ROLE_QA_API_KEY': 'secret-test'}):
            details = qa.error_details(exc, {'model_type': 'test-model'})
        self.assertEqual(details['http_status'], 400)
        self.assertEqual(details['code'], 'InvalidParameter')
        self.assertEqual(details['param'], 'model')
        serialized = json.dumps(details, ensure_ascii=False)
        self.assertNotIn('secret-test', serialized)
        self.assertNotIn('sk-test-secret', serialized)
        self.assertNotIn('异常原文', serialized)
        self.assertIn('实际得分', qa.error_message(details))
        self.assertNotIn('未填写', qa.error_message(details))

    def test_http_rejection_persists_failure_without_fabricating_score(self):
        import httpx
        from openai import BadRequestError
        response = httpx.Response(400, request=httpx.Request('POST', 'http://localhost/v1/chat/completions'))
        exc = BadRequestError('请求被拒绝', response=response, body={'code': 'ModelDenied', 'message': '模型不可调用'})
        client = MagicMock()
        client.chat.completions.create.side_effect = exc
        settings = {'url': 'http://localhost/v1', 'model_type': 'test-model',
                    'api_key_env': 'PAPER2VIDEO_ROLE_QA_API_KEY', 'model_config': {}}
        with tempfile.TemporaryDirectory(dir=ROOT / 'jobs/_validation', prefix='qa-request-') as temporary:
            with patch('openai.OpenAI', return_value=client), patch.dict(os.environ, {settings['api_key_env']: 'test-secret'}):
                with self.assertRaises(BadRequestError):
                    qa.model_request(settings, '不应写入错误日志的原文', temporary)
            details = qa.read_json(Path(temporary) / 'evaluation/last-request-error.json')
            self.assertEqual(details['code'], 'ModelDenied')
            self.assertNotIn('score', details)
            self.assertNotIn('不应写入', json.dumps(details, ensure_ascii=False))
            ledger = qa.read_json(next((Path(temporary) / 'model_usage').glob('*.json')))
            self.assertEqual(ledger['state'], 'failed')
            self.assertIsNone(ledger['estimated_cny'])
            client.close.assert_called_once()

    def test_unsupported_correct_guess_and_missing_answers_are_failures(self):
        bank = {'questions': questions()}
        result = qa.grade(bank, [{'id': '1', 'answer': 'A', 'evidence': '原文知识'},
            {'id': '2', 'answer': 'A', 'evidence': '模型常识'}, {'id': '3', 'answer': 'B', 'evidence': '原文知识'}], '材料包含原文知识')
        self.assertEqual((result['correct'], result['total'], result['score']), (1, 4, 25))
        self.assertFalse(result['items'][1]['supported'])
        with self.assertRaises(ValueError):
            qa.grade(bank, [{'id': '1'}, {'id': '1'}], '')
        with self.assertRaises(ValueError):
            qa.grade(bank, [{'id': '1', 'answer': 'A', 'evidence': {}}], '')


    def test_threshold_edges_and_retry_limit(self):
        policy = EvaluationPolicy(enabled=True)
        self.assertEqual(qa.route(80, policy), 'pass')
        self.assertEqual(qa.route(60, policy), 'review')
        self.assertEqual(qa.route(59.99, policy, 0), 'retry')
        self.assertEqual(qa.route(59.99, policy, 2), 'review')
        self.assertEqual(qa.route(79, policy, stage='script'), 'review')
        self.assertEqual(qa.route(80, policy, stage='script'), 'pass')
        with self.assertRaises((ConfigurationError, ValidationError)):
            EvaluationPolicy(minimum_threshold=90, standard_threshold=80)

    def test_source_quotes_and_missing_answer_are_rejected(self):
        qa.validate_questions(questions(), '原文知识', 4)
        invalid = questions()
        invalid[0]['answer'] = None
        with self.assertRaises(ValueError):
            qa.validate_questions(invalid, '原文知识', 4)
        invalid = questions()
        invalid[0]['source_quote'] = '原文没有的知识'
        with self.assertRaises(ValueError):
            qa.validate_questions(invalid, '原文知识', 4)

    def test_schema_compatibility_and_independent_role(self):
        old = legacy_config({})
        self.assertFalse(old.avatar.enabled)
        self.assertFalse(old.evaluation.enabled)
        value = old.public_dict()
        value['stages']['qa'] = copy.deepcopy(value['stages']['vision'])
        value['evaluation'] = {'enabled': True}
        current = PipelineModelConfig.model_validate(value)
        self.assertEqual(required_roles(current, {'stages': ['2'], 'review_before_render': True}), ['qa', 'vision'])
        self.assertIn('asr', required_roles(current, {'stages': ['2', '3'], 'reuse_script': True, 'require_reuse_script': True}))


class FlowChecks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=ROOT / 'jobs/_validation', prefix='qa-check-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for name, value in [('JOBS_ROOT', str(self.root)), ('_QUEUE', asyncio.Queue()), ('_JOB_KEYS', {})]:
            override = patch.object(web, name, value)
            override.start()
            self.addCleanup(override.stop)
        for name, value in [('_configure_job_models', {}), ('_configure_qa_models', {}), ('_job_model_environment', {})]:
            override = patch.object(web, name, return_value=value)
            override.start()
            self.addCleanup(override.stop)
        self.result = self.root / 'original/result'
        (self.result / 'slide_imgs').mkdir(parents=True)
        (self.result / 'slide_imgs/1.png').write_bytes(b'original slide')
        (self.result / 'audio').mkdir()
        (self.result / 'audio/0.wav').write_bytes(b'original audio')
        (self.result / '3_merage.mp4').write_bytes(b'original video')
        (self.result / 'subtitle_w_cursor.txt').write_text('原文知识 | no', encoding='utf-8')
        source = self.root / 'original/ingest/paper_text.md'
        source.parent.mkdir()
        source.write_text('原文知识作为出题唯一源数据', encoding='utf-8')
        config = legacy_config({}, 'mock-connection').public_dict()
        config['stages']['qa'] = copy.deepcopy(config['stages']['vision'])
        config['evaluation'] = EvaluationPolicy(enabled=True, question_count=4).model_dump()
        self.config = config
        self.job = {'id': 'original', 'status': 'awaiting_review', 'config': {'model_config': config, 'model_config_origin': 'v2',
            'result_dir': str(self.result), 'slide_lang': 'zh', 'script_lang': 'zh', 'review_before_render': True}, 'needs_struct': False}
        web._save_job(self.job)

    def report(self, stage='script', score=100):
        return {'stage': stage, 'language': 'zh', 'score': score, 'total': 4, 'correct': round(score / 25), 'fingerprint': 'a' * 64,
            'source_sha256': qa.digest_file(self.root / 'original/ingest/paper_text.md'),
            'material_sha256': qa.digest_file(self.result / ('subtitle_w_cursor.txt' if stage == 'script' else '3_merage.mp4')),
            'model_selection': self.config['stages']['qa'], 'items': [{**row, 'correct': score == 100} for row in questions()]}

    def test_stale_script_rejected_and_explicit_override_allowed(self):
        job = copy.deepcopy(self.job)
        job['evaluation'] = {'script': self.report()}
        web._qa_check_script(job, job['config'], '原文知识 | no')
        with self.assertRaises(web.HTTPException):
            web._qa_check_script(job, job['config'], '改过的知识 | no')
        web._qa_check_script(job, job['config'], '改过的知识 | no', True)
        job['config']['model_config']['stages']['qa']['model'] = 'different'
        with self.assertRaises(web.HTTPException):
            web._qa_check_script(job, job['config'], '原文知识 | no')

    def test_direct_continue_cannot_bypass_script_gate(self):
        with self.assertRaises(web.HTTPException):
            asyncio.run(web.api_rerun_job('original', {'action': 'continue'}))
        self.assertEqual(web._QUEUE.qsize(), 0)

    def test_windows_crlf_script_can_pass_but_changed_language_invalidates_report(self):
        script = self.result / 'subtitle_w_cursor.txt'
        script.write_bytes('原文知识 | no\r\n\r\n下一段知识 | no'.encode('utf-8'))
        job = copy.deepcopy(self.job)
        job['evaluation'] = {'script': self.report()}
        web._qa_check_script(job, job['config'], script.read_text(encoding='utf-8'))
        job['config']['script_lang'] = 'en'
        with self.assertRaises(web.HTTPException):
            web._qa_check_script(job, job['config'], script.read_text(encoding='utf-8'))

    def test_manual_decisions_under_job_lock_and_stale_source_are_rejected(self):
        job = web._load_job('original')
        job['config']['review_before_render'] = False
        job['evaluation'] = {'video': self.report('video', 75)}
        web._save_job(job)
        async def decide(action, fingerprint='a' * 64):
            with patch.object(web, '_local_write'), patch.object(web, '_JOB_LOCK', asyncio.Lock()):
                return await asyncio.wait_for(web.api_evaluation_decision('original',
                    web.EvaluationDecisionRequest(action=action, fingerprint=fingerprint), None), timeout=3)
        accepted = asyncio.run(decide('accept'))
        self.assertEqual(accepted['job_id'], 'original')
        self.assertEqual(web._load_job('original')['qa_decision'], 'manual_accept')
        job['status'] = 'awaiting_review'
        job['config']['review_before_render'] = True
        job['evaluation'] = {'script': self.report('script', 25)}
        web._save_job(job)
        continued = asyncio.run(decide('continue'))
        self.assertEqual(web._load_job(continued['job_id'])['config']['qa_attempt'], 0)
        self.assertEqual(web._load_job('original')['qa_decision'], 'manual_continue')
        (self.root / 'original/ingest/paper_text.md').write_text('原文已修改', encoding='utf-8')
        with self.assertRaises(web.HTTPException):
            asyncio.run(decide('continue'))

    def test_canceled_task_cannot_restart_even_before_evaluator_launch(self):
        job = web._load_job('original')
        job['status'] = 'canceled'
        web._save_job(job)
        with patch.object(web.asyncio, 'create_subprocess_exec', new_callable=AsyncMock) as process:
            asyncio.run(web._qa_evaluate_and_route(job, 'video'))
            process.assert_not_called()
        with self.assertRaises(web.HTTPException):
            asyncio.run(web._qa_start_next(job, regenerate=True, automatic=True))
        self.assertEqual(web._load_job('original')['status'], 'canceled')

    def test_evaluation_endpoint_rejects_old_video_then_queues_current_stage(self):
        async def run(stage):
            with patch.object(web, '_local_write'), patch.object(web, '_JOB_LOCK', asyncio.Lock()):
                return await asyncio.wait_for(web.api_evaluation_run('original', web.EvaluationRunRequest(stage=stage), None), 3)
        with self.assertRaises(web.HTTPException):
            asyncio.run(run('video'))
        self.assertEqual(web._QUEUE.qsize(), 0)
        response = asyncio.run(run('script'))
        self.assertEqual(response['job_id'], 'original')
        self.assertEqual(web._load_job('original')['evaluation_only'], 'script')
        self.assertEqual(web._QUEUE.get_nowait(), 'original')

    def test_standalone_evaluation_does_not_require_expired_generation_connections(self):
        store = ConnectionStore()
        current = store.create(ConnectionCreate(label='仅问答连接', provider='openai_compatible',
            compatible_base_url='http://localhost:9000/v1', api_key='qa-only-test-key'))
        job = copy.deepcopy(self.job)
        config = job['config']
        config['model_config']['stages']['qa'].update(provider='openai_compatible', connection_id=current['id'])
        config['model_config']['stages']['tts'] = {'mode': 'api', 'provider': 'bailian', 'model': 'cosyvoice-v3-plus', 'connection_id': 'expired-tts', 'parameters': {}}
        config['model_config']['timing'] = {'source': 'tts_timestamp'}
        config['model_config']['avatar']['enabled'] = True
        config['voice'] = {'kind': 'cloud', 'resource_id': 'expired-cloud-voice'}
        with patch.object(web, '_MODEL_CONNECTIONS', store):
            keys = REAL_QA_CONFIGURE(config, job, 'video')
        self.assertEqual(config['required_model_roles'], ['qa', 'asr'])
        self.assertEqual(keys[current['id']], 'qa-only-test-key')
        self.assertNotIn('expired-tts', keys)

    def test_automatic_continue_has_isolated_media_and_inherited_report(self):
        job = web._load_job('original')
        job['evaluation'] = {'script': self.report()}
        web._save_job(job)
        response = asyncio.run(web._qa_start_next(job, automatic=True))
        child = web._load_job(response['job_id'])
        self.assertNotEqual(child['config']['result_dir'], str(self.result))
        self.assertEqual(child['evaluation']['script']['score'], 100)
        branch = Path(child['config']['result_dir'])
        (branch / '3_merage.mp4').write_bytes(b'new video')
        self.assertEqual((self.result / '3_merage.mp4').read_bytes(), b'original video')
        self.assertEqual(web._load_job('original')['qa_next_job_id'], child['id'])
        self.assertEqual(web._QUEUE.get_nowait(), child['id'])

    def test_automatic_regeneration_injects_grounded_feedback_and_is_bounded(self):
        job = web._load_job('original')
        job['evaluation'] = {'video': self.report('video', 25)}
        web._save_job(job)
        response = asyncio.run(web._qa_start_next(job, regenerate=True, automatic=True))
        child = web._load_job(response['job_id'])
        self.assertEqual(child['config']['qa_attempt'], 1)
        prompt = Path(child['config']['script_prompt_path']).read_text(encoding='utf-8')
        self.assertIn('QA_FEEDBACK', prompt)
        self.assertIn('原文知识', prompt)
        self.assertFalse(child['config']['reuse_script'])
        self.assertTrue(child['config']['review_before_render'])
        self.assertEqual((self.result / 'audio/0.wav').read_bytes(), b'original audio')
        job['config']['qa_attempt'] = 2
        with self.assertRaises(web.HTTPException):
            asyncio.run(web._qa_start_next(job, regenerate=True, automatic=True))

    def test_worker_routes_valid_score_and_does_not_route_canceled_or_invalid_result(self):
        async def exercise(score, code=0, canceled=False):
            job = web._load_job('original')
            job['config']['review_before_render'] = False
            job['status'] = 'running'
            web._save_job(job)
            report = self.report('video', score)
            async def launch(*argv, **kwargs):
                qa.write_json(argv[argv.index('--output') + 1], report)
                proc = AsyncMock()
                async def wait():
                    if canceled:
                        await web._update_job('original', status='canceled')
                    return code
                proc.wait.side_effect = wait
                return proc
            with patch.object(web.asyncio, 'create_subprocess_exec', side_effect=launch), patch.object(web, '_qa_start_next', new_callable=AsyncMock) as next_job:
                await web._qa_evaluate_and_route(job, 'video')
                return web._load_job('original'), next_job.call_args
        passed, called = asyncio.run(exercise(100))
        self.assertEqual(passed['status'], 'done')
        self.assertIsNone(called)
        middle, called = asyncio.run(exercise(75))
        self.assertEqual(middle['status'], 'awaiting_review')
        self.assertIsNone(called)
        low, called = asyncio.run(exercise(25))
        self.assertTrue(called.kwargs['regenerate'])
        failed, called = asyncio.run(exercise(100, code=1))
        self.assertEqual(failed['status'], 'awaiting_review')
        self.assertTrue(failed['qa_error'])
        self.assertIsNone(called)
        canceled, called = asyncio.run(exercise(100, canceled=True))
        self.assertEqual(canceled['status'], 'canceled')
        self.assertIsNone(called)

    def test_failed_evaluation_exposes_diagnostics_and_preserves_thresholds(self):
        job = web._load_job('original')
        job['config']['model_config']['evaluation']['script_threshold'] = 85
        job['status'] = 'running'
        web._save_job(job)
        message = '评价未完成（HTTP 400）；尚未生成实际得分，已设置的通过门槛仍然有效。原因：模型不可调用'
        async def launch(*argv, **kwargs):
            qa.write_json(argv[argv.index('--output') + 1] + '.error.json', {'stage': 'script', 'message': message})
            proc = AsyncMock()
            proc.wait.return_value = 1
            return proc
        with patch.object(web.asyncio, 'create_subprocess_exec', side_effect=launch), patch.object(web, '_qa_start_next', new_callable=AsyncMock) as next_job:
            asyncio.run(web._qa_evaluate_and_route(job, 'script'))
            next_job.assert_not_called()
        result = web._load_job('original')
        self.assertEqual(result['qa_error'], message)
        self.assertEqual(result['config']['model_config']['evaluation']['script_threshold'], 85)
        self.assertNotIn('script', result.get('evaluation', {}))
        self.assertEqual(result['status'], 'awaiting_review')

    def test_old_error_file_is_not_used_for_new_failure(self):
        job = web._load_job('original')
        error_file = self.root / 'original/qa-script.json.error.json'
        qa.write_json(error_file, {'stage': 'script', 'message': '过时的错误'})
        proc = AsyncMock()
        proc.wait.return_value = 1
        with patch.object(web.asyncio, 'create_subprocess_exec', return_value=proc):
            asyncio.run(web._qa_evaluate_and_route(job, 'script'))
        self.assertFalse(error_file.exists())
        self.assertNotEqual(web._load_job('original')['qa_error'], '过时的错误')

    def test_cli_failure_writes_diagnostics_without_changing_policy(self):
        import httpx
        import runpy
        from openai import BadRequestError
        snapshot = self.root / 'snapshot.json'
        qa.write_json(snapshot, {'model_config': self.config, 'model_config_origin': 'v2', 'model_connections': {
            'mock-connection': {'compatible_base_url': 'http://localhost:1/v1', 'provider': 'openai_compatible'}}})
        original = snapshot.read_bytes()
        output = self.root / 'qa-script.json'
        source = self.root / 'original/ingest/paper_text.md'
        response = httpx.Response(400, request=httpx.Request('POST', 'http://localhost/v1/chat/completions'))
        client = MagicMock()
        client.chat.completions.create.side_effect = BadRequestError('不要输出请求体', response=response,
            body={'error': {'code': 'InvalidParameter', 'message': '不支持当前型号'}})
        argv = [str(ROOT / 'Paper2Video-main/src/qa_evaluation.py'), '--snapshot', str(snapshot), '--source', str(source),
                '--result', str(self.result), '--output', str(output), '--mode', 'script']
        with patch('openai.OpenAI', return_value=client), patch.dict(os.environ, {'PAPER2VIDEO_ROLE_QA_API_KEY': 'mock-key'}), patch.object(sys, 'argv', argv):
            with self.assertRaises(SystemExit) as exited:
                runpy.run_path(argv[0], run_name='__main__')
        self.assertEqual(exited.exception.code, 1)
        failure = qa.read_json(str(output) + '.error.json')
        self.assertEqual(failure['error']['http_status'], 400)
        self.assertEqual(failure['error']['code'], 'InvalidParameter')
        self.assertIn('不支持当前型号', failure['message'])
        self.assertFalse(output.exists())
        self.assertEqual(snapshot.read_bytes(), original)

    def test_questions_answer_separation_cache_and_material_invalidation(self):
        snapshot = self.root / 'snapshot.json'
        qa.write_json(snapshot, {'model_config': self.config, 'model_config_origin': 'v2', 'model_connections': {
            'mock-connection': {'compatible_base_url': 'http://localhost:1/v1', 'provider': 'openai_compatible'}}})
        source = self.root / 'original/ingest/paper_text.md'
        calls = []
        def request(settings, content, directory):
            calls.append(content)
            if isinstance(content, str):
                return {'questions': questions()}
            self.assertNotIn('source_quote', json.dumps(content))
            self.assertNotIn('作为出题唯一源数据', json.dumps(content, ensure_ascii=False))
            return {'answers': [{'id': str(i + 1), 'answer': 'A', 'evidence': '原文知识'} for i in range(4)]}
        with patch.dict(os.environ, {'PAPER2VIDEO_ROLE_QA_API_KEY': 'mock-key'}):
            output = self.root / 'report.json'
            report = qa.evaluate('script', snapshot, source, self.result, output, request=request)
            self.assertEqual(report['score'], 100)
            qa.evaluate('script', snapshot, source, self.result, output, request=request)
            self.assertEqual(len(calls), 2)
            (self.result / 'subtitle_w_cursor.txt').write_text('原文知识有所修改 | no', encoding='utf-8')
            new = qa.evaluate('script', snapshot, source, self.result, output, request=request)
            self.assertEqual(len(calls), 3)
            self.assertNotEqual(new['fingerprint'], report['fingerprint'])

    def test_invalid_question_response_is_preserved_before_validation(self):
        source = self.root / 'original/ingest/paper_text.md'
        settings = {'model_type': 'fixture'}
        response = {'questions': questions()}
        response['questions'][2]['options'] = {'A': '原文知识', 'B': '乙', 'C': '丙'}
        request = MagicMock(return_value=copy.deepcopy(response))
        root = self.result / 'evaluation'
        root.mkdir()
        with self.assertRaisesRegex(ValueError, '第3题.*缺少D'):
            qa.question_bank(source, root, settings, 4, request)
        request.assert_called_once()
        saved = qa.read_json(next(root.glob('*.response.json')))
        self.assertEqual(saved, response)
        self.assertEqual(list(root.glob('*-part-0.json')), [])
        self.assertEqual(list(root.glob('report-*.json')), [])

    def test_preserved_response_recovers_without_paid_request(self):
        source = self.root / 'original/ingest/paper_text.md'
        settings = {'model_type': 'fixture'}
        root = self.result / 'evaluation'
        root.mkdir()
        key = qa.digest({'source': source.read_text(encoding='utf-8').strip(), 'count': 4, 'settings': settings, 'version': 1})
        response = {'questions': questions()}
        for row in response['questions']:
            row['options'] = list(row['options'].values())
        qa.write_json(root / f'questions-{key}-part-0.response.json', response)
        request = MagicMock(side_effect=AssertionError('不应重新付费请求'))
        bank = qa.question_bank(source, root, settings, 4, request)
        self.assertEqual(bank['id'], key)
        self.assertEqual(bank['questions'][0]['options']['A'], '原文知识')
        self.assertEqual(qa.read_json(root / f'questions-{key}-part-0.response.json'), response)
        self.assertIsNotNone(qa.read_json(root / f'questions-{key}-part-0.json'))
        self.assertEqual(qa.question_bank(source, root, settings, 4, request), bank)
        request.assert_not_called()

    def test_partial_bank_retries_only_invalid_question_and_keeps_valid_ones(self):
        source = self.root / 'original/ingest/paper_text.md'
        settings = {'model_type': 'fixture'}
        root = self.result / 'evaluation'
        root.mkdir()
        original = questions()
        original[2]['options'].pop('C')
        first = MagicMock(return_value={'questions': copy.deepcopy(original)})
        with self.assertRaisesRegex(ValueError, '已保留3题，待修复1题.*第3题.*缺少C'):
            qa.question_bank(source, root, settings, 4, first)
        first.assert_called_once()
        raw = next(root.glob('*-part-0.response.json'))
        raw_hash = qa.digest_file(raw)
        fixed = questions(1)[0]
        fixed['question'] = '修复后的第三道题'
        second = MagicMock(return_value={'questions': [fixed]})
        bank = qa.question_bank(source, root, settings, 4, second)
        second.assert_called_once()
        self.assertIn('重新生成1道', second.call_args.args[1])
        self.assertEqual([q['question'] for q in bank['questions']], ['问题1', '问题2', '修复后的第三道题', '问题4'])
        self.assertEqual(qa.digest_file(raw), raw_hash)
        self.assertEqual(list(root.glob('*.pending.json')), [])
        third = MagicMock(side_effect=AssertionError('完整题库不能重复请求'))
        self.assertEqual(qa.question_bank(source, root, settings, 4, third), bank)
        third.assert_not_called()

    def test_bad_replacement_preserves_valid_slots_and_does_not_loop(self):
        source = self.root / 'original/ingest/paper_text.md'
        settings = {'model_type': 'fixture'}
        root = self.result / 'evaluation'
        root.mkdir()
        key = qa.digest({'source': source.read_text(encoding='utf-8').strip(), 'count': 4, 'settings': settings, 'version': 1})
        original = questions()
        original[2]['options'].pop('C')
        qa.write_json(root / f'questions-{key}-part-0.response.json', {'questions': original})
        bad = questions(1)[0]
        bad['question'] = '问题1'
        request = MagicMock(return_value={'questions': [bad]})
        with self.assertRaisesRegex(ValueError, '已保留3题，待修复1题.*重复'):
            qa.question_bank(source, root, settings, 4, request)
        request.assert_called_once()
        pending = qa.read_json(root / f'questions-{key}-part-0.pending.json')
        self.assertEqual(pending['questions'][0]['question'], '问题1')
        self.assertEqual(pending['questions'][3]['question'], '问题4')
        self.assertFalse((root / f'questions-{key}.json').exists())

    def test_slot_validation_collects_errors_without_altering_original(self):
        original = questions()
        original[0]['options'].pop('C')
        original[2]['source_quote'] = '不存在的原文'
        snapshot = copy.deepcopy(original)
        checked, errors = qa.question_slots(original, '原文知识', 4)
        self.assertEqual(set(errors), {0, 2})
        self.assertEqual(checked[1]['question'], '问题2')
        self.assertEqual(original, snapshot)

    def test_media_evidence_decodes_actual_video_and_caches_transcription(self):
        video = self.root / 'fixture.mp4'
        subprocess.run(['ffmpeg', '-v', 'error', '-y', '-f', 'lavfi', '-i', 'color=c=white:s=320x180:r=16',
            '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=16000', '-t', '2', '-c:v', 'libx264', '-c:a', 'aac', str(video)], check=True)
        calls = []
        def transcribe(path, lang=None):
            calls.append(path)
            self.assertTrue(Path(path).is_file())
            return '这是从成片音轨识别的知识'
        evidence = qa.media_evidence(video, self.result / 'evaluation', 'zh', transcribe)
        self.assertEqual(len(evidence['frames']), 4)
        self.assertIn('成片音轨', evidence['transcript'][0]['text'])
        qa.media_evidence(video, self.result / 'evaluation', 'zh', transcribe)
        self.assertEqual(len(calls), 1)


if __name__ == '__main__':
    unittest.main()
