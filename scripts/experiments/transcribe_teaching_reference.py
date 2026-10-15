"""转写已下载的公开教学音轨；时间戳为真实音频窗口，不冒充词级对齐。"""
import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(REPO / 'Paper2Video-main/src'), str(REPO)]
import pv_env
import course


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    args = parser.parse_args()
    import soundfile as sf
    from speech_gen import get_asr_model, release_models, ASR_REPO

    metadata = course.read_json(args.root / 'metadata.json')
    assert metadata['bvid'] == 'BV1KSh1zvEpY'
    model = None
    for page in (26, 27):
        part = next(p for p in metadata['pages'] if p['page'] == page)
        audio = args.root / f'p{page}.m4a'
        audio_sha = hashlib.sha256(audio.read_bytes()).hexdigest()
        chunk_dir = args.root / f'p{page}_chunks'
        chunk_dir.mkdir(exist_ok=True)
        target = args.root / f'p{page}_transcript.json'
        saved = course.read_json(target, {})
        identity = {'audio_sha256': audio_sha, 'model': ASR_REPO, 'chunk_seconds': 45,
                    'language': 'Chinese', 'timestamp_kind': 'audio_window_not_word_alignment'}
        if saved and saved['identity'] != identity:
            raise ValueError('输入或模型发生变化；请使用新的输出目录')
        if not list(chunk_dir.glob('*.wav')):
            subprocess.run(['ffmpeg', '-v', 'error', '-nostdin', '-i', str(audio),
                            '-ar', '16000', '-ac', '1', '-c:a', 'pcm_s16le',
                            '-f', 'segment', '-segment_time', '45', str(chunk_dir / '%03d.wav')], check=True)
        chunks = sorted(chunk_dir.glob('*.wav'))
        windows, start = [], 0
        for ordinal, filename in enumerate(chunks):
            info = sf.info(str(filename))
            end = start + info.frames / info.samplerate
            windows.append({'id': f"{part['cid']}-window-{ordinal + 1}", 'ordinal': ordinal,
                            'startMs': round(start * 1000), 'endMs': round(end * 1000),
                            'audio_file': filename.name})
            start = end
        segments = saved.get('segments', [])
        for offset in range(len(segments), len(windows), 4):
            if model is None:
                model = get_asr_model()
            selected = windows[offset:offset + 4]
            audios = [sf.read(str(chunk_dir / w['audio_file']), dtype='float32') for w in selected]
            results = model.transcribe(audio=audios, language='Chinese')
            if len(results) != len(selected):
                raise ValueError('ASR未覆盖全部音频窗口')
            for window, result in zip(selected, results):
                text = result.text.strip()
                if not text:
                    raise ValueError('空转写，保留进度等待核对')
                segments.append({**window, 'text': text})
            course.write_json(target, {'identity': identity, 'bvid': metadata['bvid'],
                              'part': part, 'origin': 'local_qwen3_asr',
                              'segments': segments, 'completed': len(segments) == len(windows)})
            print(f'P{page} {len(segments)}/{len(windows)} windows', flush=True)
        course.write_text(args.root / f'p{page}_transcript.txt', '\n\n'.join(
            f"[{s['id']} {s['startMs']/1000:.2f}-{s['endMs']/1000:.2f}s]\n{s['text']}" for s in segments))
    release_models(('asr',))


if __name__ == '__main__':
    main()
