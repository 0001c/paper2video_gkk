"""Offline resume regression. No weights, GPU inference or cloud requests.

Run: .venv/Scripts/python.exe scripts/check_audio_resume.py
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
import soundfile as sf
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'Paper2Video-main/src'))
import cursor_gen
import speech_gen


class ResumeChecks(unittest.TestCase):
    def setUp(self):
        (ROOT / 'jobs/_validation').mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix='audio-resume-', dir=ROOT / 'jobs/_validation')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.audio = self.root / 'audio'
        self.script = self.root / 'script.txt'
        self.script.write_text('First page | no\n###\nSecond page | no\n###\nThird page | no', encoding='utf-8')
        self.reference = self.root / 'reference.wav'
        sf.write(self.reference, np.ones(2400) * 0.1, 24000)

    def synthesize(self, text, output, *args, **kwargs):
        sf.write(output, np.ones(2400) * 0.2, 24000)
        return output, 0.1

    def run_tts(self, ref_text='Reference text', language='English'):
        speech_gen.tts_per_slide('qwen3tts', str(self.script), str(self.audio), str(self.reference),
                                 ref_text, language=language)

    def test_failure_resumes_completed_pages_and_validates_changes(self):
        calls = []
        def interrupted(text, output, *args, **kwargs):
            calls.append(text)
            if len(calls) == 2:
                raise RuntimeError('interrupted synthesis')
            return self.synthesize(text, output)
        with patch.object(speech_gen, 'synth_page_qwen3tts', side_effect=interrupted):
            with self.assertRaisesRegex(RuntimeError, 'interrupted'):
                self.run_tts()
        first = self.audio / '0.wav'
        old = (first.read_bytes(), first.stat().st_mtime_ns)
        with patch.object(speech_gen, 'synth_page_qwen3tts', side_effect=self.synthesize) as synth:
            self.run_tts()
            self.assertEqual(synth.call_count, 2)
        self.assertEqual(old, (first.read_bytes(), first.stat().st_mtime_ns))
        with patch.object(speech_gen, 'synth_page_qwen3tts', side_effect=AssertionError('must reuse')), \
                patch.object(speech_gen, 'get_tts_model', side_effect=AssertionError('must not load weights')):
            self.run_tts()
        self.script.write_text('Changed first page | no\n###\nSecond page | no\n###\nThird page | no', encoding='utf-8')
        with patch.object(speech_gen, 'synth_page_qwen3tts', side_effect=self.synthesize) as synth:
            self.run_tts()
            self.assertEqual(synth.call_count, 1)
        first.write_bytes(b'corrupted')
        status = speech_gen.local_audio_status(str(self.script), str(self.audio), str(self.reference), 'Reference text', 'English')
        self.assertEqual(status['reusable_pages'], 2)
        # Rebuild the corrupt page from validated completed chunks, without TTS.
        with patch.object(speech_gen, 'synth_page_qwen3tts', side_effect=AssertionError('chunks must reuse')):
            self.run_tts()
        with patch.object(speech_gen, 'synth_page_qwen3tts', side_effect=self.synthesize) as synth:
            self.run_tts(ref_text='Different voice reference')
            self.assertEqual(synth.call_count, 3)
        with patch.object(speech_gen, 'synth_page_qwen3tts', side_effect=self.synthesize) as synth:
            self.run_tts(ref_text='Different voice reference', language='Chinese')
            self.assertEqual(synth.call_count, 3)

    def test_reference_transcript_is_reused_across_retries(self):
        with patch.object(speech_gen, 'transcribe_with_qwen3asr', return_value='Transcribed reference') as asr, \
                patch.object(speech_gen, 'synth_page_qwen3tts', side_effect=self.synthesize) as synth:
            self.run_tts(ref_text=None)
            self.run_tts(ref_text=None)
            self.assertEqual(asr.call_count, 1)
            self.assertEqual(synth.call_count, 3)
            sf.write(self.reference, np.ones(2400) * 0.3, 24000)
            self.run_tts(ref_text=None)
            self.assertEqual(asr.call_count, 2)
            self.assertEqual(synth.call_count, 6)

    def test_grounding_image_is_atomic_and_task_specific(self):
        images = []
        for name, color in [('task-a', 'red'), ('task-b', 'blue')]:
            directory = self.root / name
            directory.mkdir()
            source = directory / '8.png'
            Image.new('RGB', (2016, 1134), color).save(source)
            images.append(source)
        blocked = self.root / 'unused-external-directory'
        with patch.object(cursor_gen.pv_env, 'TMP_DIR', str(blocked)):
            outputs = [Path(cursor_gen.prepare_grounding_image(source)) for source in images]
        self.assertFalse(blocked.exists())
        self.assertNotEqual(outputs[0], outputs[1])
        for output, color in zip(outputs, [(255, 0, 0), (0, 0, 255)]):
            with Image.open(output) as bitmap:
                self.assertEqual(bitmap.size, (1008, 567))
                self.assertEqual(bitmap.getpixel((10, 10)), color)
        old_mtime = outputs[0].stat().st_mtime_ns
        self.assertEqual(Path(cursor_gen.prepare_grounding_image(images[0])), outputs[0])
        self.assertEqual(old_mtime, outputs[0].stat().st_mtime_ns)
        outputs[0].write_bytes(b'broken cache')
        self.assertEqual(Path(cursor_gen.prepare_grounding_image(images[0])), outputs[0])
        Image.new('RGB', (2016, 1134), 'green').save(images[0])
        self.assertNotEqual(Path(cursor_gen.prepare_grounding_image(images[0])), outputs[0])
        self.assertEqual(len(list(outputs[0].parent.glob('tmp*.png'))), 0)


if __name__ == '__main__':
    unittest.main()
