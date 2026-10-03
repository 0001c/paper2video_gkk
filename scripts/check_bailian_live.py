"""授权实测入口：stdin瞬时凭据；只传合成测试图/短文本，禁止自动重试。"""

import json
import os
import sys
import wave
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'Paper2Video-main' / 'src')]

import httpx
from PIL import Image, ImageDraw
from camel.messages import BaseMessage
import avatar_segments
import bailian_speech
import cursor_gen
import wei_utils
from web import model_costs
from web.model_config import legacy_config
from web.model_connections import checked_url


def run(credentials):
    base = checked_url(credentials['base_url'], 'bailian').rstrip('/')
    key = credentials['api_key'].strip()
    if not key or credentials.get('authorize_paid_requests') is not True:
        raise ValueError('实测必须提供瞬时凭据并显式授权收费请求')
    native = base.removesuffix('/compatible-mode/v1') + '/api/v1'
    bailian_speech.native_base({'native_http_base_url': native})
    destination = Path(credentials['output_directory']).resolve() if credentials.get('output_directory') else ROOT / 'jobs' / '_validation' / datetime.now(timezone.utc).strftime('%Y-%m-%d_%H-%M-%S_UTC')
    if not destination.is_relative_to((ROOT / 'jobs' / '_validation').resolve()):
        raise ValueError('实测产物目录必须位于jobs/_validation')
    destination.mkdir(parents=True, exist_ok=True)
    os.environ['TIKTOKEN_CACHE_DIR'] = str(destination / 'tokenizer-cache')
    previous_checks = {}
    if (destination / 'report.json').exists():
        previous = json.loads((destination / 'report.json').read_text(encoding='utf-8'))
        previous_checks = {item['name']: item for item in previous['checks'] if item['passed']}
        (destination / 'report.json').replace(destination / ('report-before-' + datetime.now(timezone.utc).strftime('%H-%M-%S') + '.json'))
    os.environ['PAPER2VIDEO_USAGE_DIR'] = str(destination)
    os.environ['PAPER2VIDEO_MODEL_CONFIG_ORIGIN'] = 'v2'
    for role in ('text', 'vision', 'grounding', 'tts'):
        os.environ[f'PAPER2VIDEO_ROLE_{role.upper()}_API_KEY'] = key
    config = legacy_config({'generate_talking': False}, 'live')
    for role, model in [('text', 'qwen3.8-flash'), ('vision', 'qwen3-vl-plus'), ('grounding', 'qwen3-vl-plus'), ('tts', 'cosyvoice-v3-flash')]:
        stage = config.roles()[role]
        stage.mode, stage.provider, stage.model, stage.connection_id = 'api', 'bailian', model, 'live'
    config.stages.text.parameters.max_output_tokens = 128
    config.stages.vision.parameters.max_output_tokens = 256
    config.timing.source = 'tts_timestamp'
    connection = {'provider': 'bailian', 'scope_id': 'authorized-validation', 'compatible_base_url': base, 'native_http_base_url': native}
    snapshot = {'model_config_origin': 'v2', 'model_config': config.public_dict(), 'model_connections': {'live': connection},
                'voice_resource': {'target_model': 'cosyvoice-v3-flash', 'scope_id': connection['scope_id'], 'native_http_base_url': native,
                    'voice_id': 'longanyang', 'status': 'OK', 'timestamp_status': 'supported', 'language': 'zh'}}
    image_directory, audio_directory = destination / 'images', destination / credentials.get('audio_directory', 'audio')
    if audio_directory.parent != destination or not audio_directory.name.startswith('audio'):
        raise ValueError('实测音频目录无效')
    image_directory.mkdir(exist_ok=True); audio_directory.mkdir(exist_ok=True)
    image_path = image_directory / '1.png'
    image = Image.new('RGB', (800, 600), 'white')
    draw = ImageDraw.Draw(image)
    draw.rectangle((180, 140, 420, 380), fill='red')
    draw.text((255, 245), 'TARGET', fill='white')
    image.save(image_path)
    text = '你好，这是百炼接口测试。'
    script = destination / 'script.txt'
    script.write_text(text + '|Locate the center of the red square labeled TARGET.', encoding='utf-8')
    report = {'created_at': datetime.now(timezone.utc).isoformat(), 'output_directory': str(destination), 'checks': [],
              'scope': '目录GET、一次文字、一次视觉、一次短配音、一次定位；缓存复用及云音频派生45秒分段，不运行GPU/数字人口型/Wan/OSS。'}
    def check(name, action):
        if credentials.get('reuse_succeeded_checks') and name in ('text', 'vision') and name in previous_checks:
            report['checks'].append({**previous_checks[name], 'reused_prior_success': True})
            print(name + ': reused prior live success', flush=True)
            return
        try:
            details = action()
            report['checks'].append({'name': name, 'passed': True, 'details': details})
            print(name + ': passed', flush=True)
        except Exception as error:
            safe = str(error) if isinstance(error, ValueError) and type(error).__module__ == 'builtins' else type(error).__name__
            report['checks'].append({'name': name, 'passed': False, 'error': safe[:200], 'http_status': getattr(error, 'status_code', None) or getattr(getattr(error, 'response', None), 'status_code', None)})
            print(name + ': failed (' + type(error).__name__ + ')', flush=True)
        report['costs'] = model_costs.summary(destination)
        bailian_speech.atomic_json(destination / 'report.json', report)

    def directory():
        response = httpx.get(base + '/models', headers={'Authorization': 'Bearer ' + key}, timeout=30, follow_redirects=False)
        response.raise_for_status()
        ids = {item['id'] for item in response.json().get('data', [])}
        return {'count_first_page': len(ids), 'requested_models_visible': {model: model in ids for model in ('qwen3.8-flash', 'qwen3-vl-plus', 'cosyvoice-v3-flash')}}
    check('model_directory', directory)
    def agent_call(role):
        settings = wei_utils.get_agent_config(config.roles()[role].model, role, snapshot)
        agent = wei_utils.create_agent(settings)
        message = BaseMessage.make_user_message(role_name='user', content='Reply with a short confirmation.' if role == 'text' else 'Describe the colored shape in this image in one short sentence.', image_list=[] if role == 'text' else [image], meta_dict={})
        response = agent.step(message)
        if not getattr(getattr(response, 'msg', None), 'content', ''):
            raise ValueError('模型未返回可用文本')
        return {'model': config.roles()[role].model, 'usage': (response.info or {}).get('usage')}
    check('text', lambda: agent_call('text'))
    check('vision', lambda: agent_call('vision'))
    def tts():
        original_validate = bailian_speech.validate_timing
        def capture(timing, pcm, original_text):
            (destination / 'synthetic-tts-diagnostic.wav').write_bytes(bailian_speech.wav_bytes(pcm))
            bailian_speech.atomic_json(destination / 'synthetic-tts-diagnostic.json', {'timing': timing, 'duration_seconds': len(pcm) / 48000, 'text': original_text})
            original_validate(timing, pcm, original_text)
        bailian_speech.validate_timing = capture
        try:
            bailian_speech.tts_pages([[(text, 'red square')]], audio_directory, snapshot)
        finally:
            bailian_speech.validate_timing = original_validate
        pcm, timing = bailian_speech.read_pair(audio_directory / '0.wav', text)
        return {'model': 'cosyvoice-v3-flash', 'duration_seconds': len(pcm) / 48000, 'timestamp_words': sum(len(segment['words']) for segment in timing['segments'])}
    check('tts_timestamps', tts)
    def cursor():
        output = str(destination / 'cursor.json')
        cursor_gen.cursor_gen_per_sentence(str(script), str(image_directory), str(audio_directory), output, resume_cache=True, timing_source='tts_timestamp', model_snapshot=snapshot)
        points = json.loads(Path(output).read_text(encoding='utf-8'))
        point = points[0]['cursor']
        if not 160 <= point[0] <= 440 or not 120 <= point[1] <= 400:
            raise ValueError('定位未落入合成图目标区域')
        before = model_costs.summary(destination)['stages']
        cursor_gen.cursor_gen_per_sentence(str(script), str(image_directory), str(audio_directory), output, resume_cache=True, timing_source='tts_timestamp', model_snapshot=snapshot)
        if model_costs.summary(destination)['stages'] != before:
            raise ValueError('重复执行定位仍发生云调用')
        return {'point': point, 'cache_no_additional_requests': True}
    check('grounding_and_cache', cursor)
    def long_segments():
        pcm, timing = bailian_speech.read_pair(audio_directory / '0.wav', text)
        samples = 45 * 24000
        extended = (pcm * (samples * 2 // len(pcm) + 1))[:samples * 2]
        long_audio = destination / 'cloud_clip_repeated_45_seconds.wav'
        long_audio.write_bytes(bailian_speech.wav_bytes(extended))
        manifest = avatar_segments.prepare(long_audio, destination / 'segments')
        rebuilt = b''.join(avatar_segments.audio_data(segment['file'])[4] for segment in manifest['segments'])
        if rebuilt != extended:
            raise ValueError('分段存在遗漏或重叠采样')
        return {'source': '云短音频在本地重复为45秒测试素材，不是额外收费的长TTS请求',
                'segment_seconds': [avatar_segments.verify_segment(segment['file']) for segment in manifest['segments']], 'samples_preserved': samples}
    check('p6_cloud_audio_derived_segments', long_segments)
    report['all_passed'] = all(item['passed'] for item in report['checks'])
    report['costs'] = model_costs.summary(destination)
    bailian_speech.atomic_json(destination / 'report.json', report)
    for role in ('text', 'vision', 'grounding', 'tts'):
        os.environ.pop(f'PAPER2VIDEO_ROLE_{role.upper()}_API_KEY', None)
    print(json.dumps(report, ensure_ascii=False), flush=True)
    return report['all_passed']


if __name__ == '__main__':
    inputs = json.loads(sys.stdin.readline())
    sys.exit(0 if run(inputs) else 1)
