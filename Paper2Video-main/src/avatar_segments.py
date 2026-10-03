"""仅数字人开启时派生严格小于20秒的连续PCM片段。"""

import hashlib
import json
import math
import subprocess
import struct
import wave
from pathlib import Path

from bailian_speech import atomic_json


def audio_data(filename):
    with wave.open(str(filename), 'rb') as handle:
        rate, channels, width, count = handle.getframerate(), handle.getnchannels(), handle.getsampwidth(), handle.getnframes()
        pcm = handle.readframes(count)
    if rate <= 0 or channels not in (1, 2) or width != 2 or count <= 0 or len(pcm) != count * channels * width:
        raise ValueError('数字人分段要求完整16-bit单/双声道PCM WAV')
    return rate, channels, width, count, pcm


def verify_segment(filename):
    rate, channels, width, count, pcm = audio_data(filename)
    if count >= 20 * rate:
        raise ValueError('数字人单段实际采样时长必须严格小于20秒')
    return count / rate


def quiet_cut(pcm, target, rate, channels):
    window = max(1, rate // 100)
    candidates = []
    for offset in [0, *range(-rate // 3, rate // 3 + 1, window)]:
        position = target + offset
        first, last = position - window // 2, position + window // 2
        if first < 0 or last * channels * 2 > len(pcm):
            continue
        samples = struct.unpack('<' + 'h' * ((last - first) * channels), pcm[first * channels * 2:last * channels * 2])
        if samples and max(abs(sample) for sample in samples) < 256:
            candidates.append(position)
    return min(candidates, key=lambda position: abs(position - target)) if candidates else target


def prepare(audio_path, directory, enabled=True, timing=None):
    if not enabled:
        return None
    source = Path(audio_path)
    rate, channels, width, count, pcm = audio_data(source)
    sha = hashlib.sha256(source.read_bytes()).hexdigest()
    words = [word for segment in (timing or {}).get('segments', []) for word in segment.get('words', [])]
    candidates = sorted({round(word['end'] * rate) for word in words if isinstance(word.get('end'), (int, float)) and math.isfinite(word['end']) and 0 < word['end'] * rate < count})
    identity = hashlib.sha256(json.dumps({'v': 1, 'audio': sha, 'timing': timing}, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    root = Path(directory) / identity
    root.mkdir(parents=True, exist_ok=True)
    cuts, start = [0], 0
    while count - start >= 20 * rate:
        target = start + 18 * rate
        nearby = [value for value in candidates if target - rate <= value <= target + rate and value > start and value - start < 20 * rate]
        end = min(nearby, key=lambda value: abs(value - target)) if nearby else quiet_cut(pcm, target, rate, channels)
        cuts.append(end)
        start = end
    cuts.append(count)
    if len(cuts) > 2 and cuts[-1] - cuts[-2] < rate // 2 and cuts[-1] - cuts[-3] < 20 * rate:
        cuts.pop(-2)
    normalized_count = 0
    indexed_words = []
    from bailian_speech import plain_text
    original = (timing or {}).get('text', '')
    normalization_map = (timing or {}).get('normalization_map', [])
    expanded_map = [item['original_index'] for item in normalization_map for character in item['normalized']]
    for word in words:
        end_index = normalized_count + len(plain_text(word.get('word', '')))
        indexed_words.append((word, normalized_count, end_index))
        normalized_count = end_index
    segments = []
    for index, (start, end) in enumerate(zip(cuts, cuts[1:])):
        filename = root / ('{:04d}.wav'.format(index))
        body = pcm[start * channels * width:end * channels * width]
        valid = False
        if filename.exists():
            try:
                cached = audio_data(filename)
                valid = cached[:4] == (rate, channels, width, end - start) and cached[4] == body
            except (OSError, ValueError, wave.Error, EOFError):
                pass
        if not valid:
            temporary = filename.with_suffix('.tmp')
            with wave.open(str(temporary), 'wb') as handle:
                handle.setnchannels(channels)
                handle.setsampwidth(width)
                handle.setframerate(rate)
                handle.writeframes(body)
            temporary.replace(filename)
        verify_segment(filename)
        overlapping = [(first, last) for word, first, last in indexed_words if word['end'] * rate > start and word['start'] * rate < end]
        text_range = None
        if overlapping and expanded_map and normalized_count == len(expanded_map):
            first, last = min(item[0] for item in overlapping), max(item[1] for item in overlapping)
            if last > first:
                text_range = [expanded_map[first], expanded_map[last - 1] + 1]
        segments.append({'index': index, 'file': str(filename.resolve()), 'start_sample': start, 'end_sample': end,
                         'sample_count': end - start, 'start_seconds': start / rate, 'end_seconds': end / rate,
                         'text_range': text_range, 'text': original[slice(*text_range)] if text_range else None,
                         'sha256': hashlib.sha256(filename.read_bytes()).hexdigest()})
    result = {'schema_version': 1, 'identity': identity, 'source_sha256': sha, 'sample_rate': rate,
              'channels': channels, 'sample_count': count, 'max_seconds_exclusive': 20, 'target_seconds': 18, 'segments': segments}
    atomic_json(root / 'manifest.json', result)
    return result


def stitch(videos, manifest, master_audio, output, fps=25):
    if len(videos) != len(manifest['segments']):
        raise ValueError('数字人视频片段缺失')
    for segment in manifest['segments']:
        verify_segment(segment['file'])
    if hashlib.sha256(Path(master_audio).read_bytes()).hexdigest() != manifest['source_sha256']:
        raise ValueError('数字人合片主音频已变化')
    arguments = ['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y']
    filters, labels = [], []
    for index, (video, segment) in enumerate(zip(videos, manifest['segments'])):
        probe = subprocess.run(['ffprobe', '-v', 'error', '-select_streams', 'v:0', '-show_entries', 'stream=duration', '-of', 'json', str(video)], capture_output=True, check=True)
        try:
            duration = float(json.loads(probe.stdout)['streams'][0]['duration'])
        except (ValueError, KeyError, IndexError):
            raise ValueError('数字人片段缺少有效视频时长') from None
        expected = segment['sample_count'] / manifest['sample_rate']
        if not math.isfinite(duration) or duration < expected - 0.15:
            raise ValueError('数字人视频片段明显短于对应音频，禁止补帧掩盖缺失')
        frames = round(segment['end_seconds'] * fps) - round(segment['start_seconds'] * fps)
        if frames < 1:
            raise ValueError('数字人片段不足一帧')
        arguments += ['-i', str(video)]
        filters.append('[{}:v]fps={},tpad=stop_mode=clone:stop_duration=0.2,trim=end_frame={},setpts=PTS-STARTPTS[v{}]'.format(index, fps, frames, index))
        labels.append('[v{}]'.format(index))
    arguments += ['-i', str(master_audio)]
    filters.append('{}concat=n={}:v=1:a=0[v]'.format(''.join(labels), len(videos)))
    target = Path(output)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.stem + '.tmp.mp4')
    total = manifest['sample_count'] / manifest['sample_rate']
    arguments += ['-filter_complex', ';'.join(filters), '-map', '[v]', '-map', '{}:a:0'.format(len(videos)),
                  '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-t', str(total), str(temporary)]
    subprocess.run(arguments, capture_output=True, check=True)
    probe = subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'format=duration:stream=codec_type', '-of', 'json', str(temporary)], capture_output=True, check=True)
    data = json.loads(probe.stdout)
    if abs(float(data['format']['duration']) - total) > 0.1 or sum(item['codec_type'] == 'audio' for item in data['streams']) != 1:
        raise ValueError('数字人合片时长或主音轨校验失败')
    temporary.replace(target)
    return str(target)
