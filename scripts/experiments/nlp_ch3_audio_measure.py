"""第三章独立音频时长测量，不生成/伪造光标时间戳，不放行生产门禁。

首次正式配音因云时间戳漏字失败后使用。原实验输入与讲稿不变；沿用
相同口述展开、分块、语速和间隔。已返回的失败块仅复用诊断里的帧数，
不重发；其余块关闭词时间戳，保存 PCM/WAV 并按帧计时。
"""
import argparse
import hashlib
import json
import threading
import wave
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import nlp_ch3_compare as experiment
import bailian_speech as speech

LOCK = threading.Lock()


def digest(value):
    return hashlib.sha256(value).hexdigest()


def measure_chunk(text, root, client, model, voice, rate, language):
    identity = dict(model=model, voice=voice, text=text, rate=rate, language=language)
    old_key = experiment.course.digest({**identity, 'timestamps': True})
    old_path = root/'tts_requests'/f'{old_key}.json'
    old = experiment.course.read_json(old_path, {})
    if old:
        if old.get('state') == 'succeeded':
            pcm, _ = speech.read_pair(old_path.with_suffix('.wav'), text)
            return {'samples': len(pcm)//2, 'origin': 'validated_original_wav',
                    'request': old_key, 'text_sha256': speech.digest(text)}, pcm
        matches = [experiment.course.read_json(p) for p in (root/'tts_diagnostics').glob('*.json')]
        matches = [d for d in matches if d.get('text_sha256') == speech.digest(text)
                   and d.get('state') == 'mapping_failed']
        if len(matches) != 1:
            raise ValueError('旧配音受理未知，禁止重发或推测时长')
        diagnostic = matches[0]
        usage = experiment.course.read_json(root/'model_usage'/(diagnostic['usage_record_id']+'.json'))
        if not usage or usage['state'] != 'succeeded' or not diagnostic['sample_count']:
            raise ValueError('失败块缺少完整返回证据')
        return {'samples': diagnostic['sample_count'], 'origin': 'mapping_failed_pcm_diagnostic',
                'request': old_key, 'usage_record_id': diagnostic['usage_record_id'],
                'text_sha256': speech.digest(text)}, None
    key = experiment.course.digest({**identity, 'timestamps': False})
    request = root/'tts_requests'/f'{key}.json'
    filename = request.with_suffix('.wav')
    with LOCK:
        saved = experiment.course.read_json(request, {})
        if saved:
            if saved.get('state') != 'succeeded' or saved.get('measurement_mode') != 'duration_only':
                raise ValueError('时长测量受理未知，禁止自动重发')
            body = filename.read_bytes()
            if digest(body) != saved['wav_sha256']:
                raise ValueError('测量缓存音频哈希不一致')
            with wave.open(str(filename), 'rb') as handle:
                if (handle.getframerate(), handle.getnchannels(), handle.getsampwidth()) != (speech.SAMPLE_RATE, 1, 2):
                    raise ValueError('测量缓存采样格式不一致')
                pcm = handle.readframes(handle.getnframes())
            if saved['sample_count'] != len(pcm)//2:
                raise ValueError('测量缓存帧数不一致')
            return {'samples': len(pcm)//2, 'origin': 'duration_only_wav', 'request': key,
                    'text_sha256': speech.digest(text)}, pcm
        records = [experiment.course.read_json(p) for p in request.parent.glob('*.json')
                   if not p.name.endswith('.timing.json')]
        if len(records) >= 200 or sum(r['characters'] for r in records)+len(text) > 45000:
            raise ValueError('达到用户授权的全部配音请求或字符上限')
        experiment.dump(request, {'state': 'submission_unknown', 'identity': key, 'characters': len(text),
                                 'model': model, 'measurement_mode': 'duration_only'})
    pcm, _ = client.synthesize(model, voice, text, rate, language, timestamps=False)
    body = speech.wav_bytes(pcm)
    filename.write_bytes(body)
    experiment.dump(request, {'state': 'succeeded', 'identity': key, 'characters': len(text), 'model': model,
                             'measurement_mode': 'duration_only', 'sample_count': len(pcm)//2,
                             'wav_sha256': digest(body)})
    return {'samples': len(pcm)//2, 'origin': 'duration_only_wav', 'request': key,
            'text_sha256': speech.digest(text)}, pcm


def measure(labels):
    experiment.check()
    # 全批次先核对完成状态，再读取凭据和开始收费请求。
    scripts = {label: experiment.parse_script((experiment.ROOT/label/'subtitle_w_cursor.txt').read_text('utf-8'))
               for label in labels}
    if any(len(rows) != 8 for rows in scripts.values()):
        raise ValueError('待测讲稿不完整')
    entry = experiment.credential()
    snapshot = experiment.course.read_json(experiment.ROOT/'tts_snapshot.json')
    stage = snapshot['model_config']['stages']['tts']
    voice = snapshot['voice_resource']
    client = speech.SpeechClient(entry, entry['api_key'], usage_directory=experiment.ROOT,
                                 usage_role='tts-duration-experiment')
    for label, rows in scripts.items():
        directory = experiment.ROOT/label/'duration_audio'
        directory.mkdir(exist_ok=True)
        pages = []
        for index, lines in enumerate(rows):
            original = '\n\n\n'.join(prompt for prompt, _ in lines)
            spoken, _ = speech.prepare_spoken(original, voice.get('language', 'zh'))
            chunks = list(speech.chunks(spoken))
            # 有界三个并发请求，预算预留受同一锁保护；不自动重试。
            with ThreadPoolExecutor(max_workers=3) as executor:
                results = list(executor.map(lambda text: measure_chunk(text, experiment.ROOT, client,
                    stage['model'], voice['voice_id'], stage['parameters'].get('speech_rate', 1.0),
                    voice.get('language', 'zh')), chunks))
            gap_samples = int(speech.SAMPLE_RATE*speech.GAP_SECONDS)
            samples = sum(record['samples'] for record, _ in results)+max(0,len(results)-1)*gap_samples
            complete_audio = all(pcm is not None for _, pcm in results)
            if complete_audio:
                combined = bytes(gap_samples*2).join(pcm for _, pcm in results)
                assert len(combined)//2 == samples
                (directory/f'{index}.wav').write_bytes(speech.wav_bytes(combined))
            pages.append({'page': index+1, 'sample_count': samples, 'seconds': samples/speech.SAMPLE_RATE,
                          'complete_wav_retained': complete_audio, 'chunks': [r for r, _ in results],
                          'script_sha256': speech.digest(original), 'spoken_sha256': speech.digest(spoken)})
            experiment.dump(directory/'pages.json', pages)
            print(f'DURATION {label} page={index+1} seconds={samples/speech.SAMPLE_RATE:.3f}', flush=True)
        minutes = sum(p['sample_count'] for p in pages)/speech.SAMPLE_RATE/60
        report = {'label': label, 'mode': 'duration_only', 'actual_minutes': minutes,
                  'within_total_10_percent': abs(minutes-experiment.TARGET) <= experiment.TARGET*.1,
                  'target_minutes': experiment.TARGET, 'sample_rate': speech.SAMPLE_RATE,
                  'voice': voice['voice_id'], 'model': stage['model'], 'rate': stage['parameters'].get('speech_rate', 1.0),
                  'gap_seconds': speech.GAP_SECONDS, 'pages': pages, 'production_alignment_passed': False,
                  'limitation': '关闭词时间戳；旧失败块仅保留返回诊断帧数，不具备完整试听与对齐证据'}
        experiment.dump(directory/'report.json', report)
        experiment.dump(experiment.OUT/f'{label}_独立音频时长.json', report)
        print(f'DURATION {label} minutes={minutes:.4f} within_10_percent={report["within_total_10_percent"]}', flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('labels', nargs='+', choices=experiment.ORDER)
    measure(parser.parse_args().labels)
