"""Offline Wan tail guard and local word-time regression; no paid calls."""
import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'Paper2Video-main/src')]
import avatar_segments as segments
import bailian_avatar as avatar


class TailChecks(unittest.TestCase):
    def setUp(self):
        (ROOT / 'jobs/_validation').mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix='wan-tail-', dir=ROOT / 'jobs/_validation')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def wav(self, name, seconds, voiced_until=0):
        filename = self.root / name
        count = round(seconds * 24000)
        pcm = b'\x00\x10' * round(voiced_until * 24000)
        pcm += bytes(count * 2 - len(pcm))
        with wave.open(str(filename), 'wb') as handle:
            handle.setparams((1, 2, 24000, 0, 'NONE', 'not compressed'))
            handle.writeframes(pcm)
        return filename

    def test_guard_preserves_original_pcm_and_strict_upload_limit(self):
        audio = self.wav('source.wav', 3.9033333333, 3.8)
        before = audio.read_bytes(), audio.stat().st_mtime_ns
        output = avatar.guarded_audio(audio, self.root / 'upload.wav')
        original, padded = segments.audio_data(audio), segments.audio_data(output)
        self.assertEqual(padded[3], original[3] + 12000)
        self.assertEqual(padded[4], original[4] + bytes(24000))
        self.assertEqual((audio.read_bytes(), audio.stat().st_mtime_ns), before)
        with self.assertRaisesRegex(ValueError, '严格小于20秒'):
            avatar.guarded_audio(self.wav('too-long.wav', 19.5), self.root / 'bad.wav')
        source = self.wav('nineteen.wav', 19.8)
        manifest = segments.prepare(source, self.root / 'parts', max_seconds=19.5)
        self.assertEqual(b''.join(segments.audio_data(s['file'])[4] for s in manifest['segments']), segments.audio_data(source)[4])
        for s in manifest['segments']:
            self.assertLess(segments.verify_segment(avatar.guarded_audio(s['file'], self.root / f"upload-{s['index']}.wav")), 20)

    def test_unchanged_cuts_keep_existing_remote_cache_identity(self):
        audio = self.wav('source.wav', 21.76)
        old = segments.prepare(audio, self.root / 'parts')
        new = segments.prepare(audio, self.root / 'parts', max_seconds=19.5)
        self.assertEqual(old['identity'], new['identity'])
        self.assertEqual(old['segments'], new['segments'])

    def test_local_word_times_allow_only_verified_silent_tail(self):
        audio = self.wav('source.wav', 6.32, 5.9)
        before = hashlib.sha256(audio.read_bytes()).hexdigest()
        segments.write_alignment_timing(audio, [{'raw_text': '讲话', 'word_spans': [
            {'text_start': 0, 'text_end': 2, 'start': 0, 'end': 5.9}]}])
        manifest = segments.prepare(audio, self.root / 'parts')
        video = self.root / 'video.mp4'
        subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y', '-f', 'lavfi',
            '-i', 'color=c=blue:s=64x64:r=16', '-t', '6.125', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', str(video)],
            check=True, capture_output=True, timeout=30)
        output = segments.stitch([video], manifest, audio, self.root / 'stitched.mp4')
        self.assertTrue(Path(output).is_file())
        self.assertEqual(hashlib.sha256(audio.read_bytes()).hexdigest(), before)
        segments.write_alignment_timing(audio, [{'raw_text': '讲话', 'word_spans': [
            {'text_start': 0, 'text_end': 2, 'start': 0, 'end': 6.2}]}])
        with self.assertRaisesRegex(ValueError, '禁止补帧'):
            segments.stitch([video], manifest, audio, self.root / 'voiced.mp4')
        sidecar = audio.with_suffix('.alignment.json')
        data = json.loads(sidecar.read_text(encoding='utf-8'))
        data['audio_sha256'] = 'wrong'
        sidecar.write_text(json.dumps(data), encoding='utf-8')
        with self.assertRaisesRegex(ValueError, '不匹配'):
            segments.word_times(audio)


if __name__ == '__main__':
    unittest.main(verbosity=2)
