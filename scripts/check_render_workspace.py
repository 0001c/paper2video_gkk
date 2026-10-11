"""真实小视频回归：全局临时目录不可用时，光标与字幕仍使用任务产物目录。"""
import json
import io
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'Paper2Video-main/src'))
import cursor_render
import cursor_styles
import pv_env
import subtitle_render


class RenderWorkspaceChecks(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=ROOT / 'jobs/_validation', prefix='render-workspace-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / '中文任务产物'
        self.root.mkdir()
        blocked = self.root / 'unavailable-global-tmp'
        blocked.write_text('这个路径不能作为临时目录', encoding='utf-8')
        setting = patch.object(pv_env, 'TMP_DIR', str(blocked))
        setting.start()
        self.addCleanup(setting.stop)
        self.source = self.root / 'source.mp4'
        subprocess.run(['ffmpeg', '-y', '-v', 'error', '-f', 'lavfi', '-i', 'color=c=blue:s=640x360:r=10:d=1',
                        '-f', 'lavfi', '-i', 'sine=frequency=440:duration=1', '-c:v', 'libx264',
                        '-pix_fmt', 'yuv420p', '-c:a', 'aac', str(self.source)], check=True)
        self.asset, self.hotspot = cursor_styles.resolve('hand-white')

    def render_cursor(self):
        target = self.root / 'cursor.mp4'
        cursor_render.render_cursor_on_video(str(self.source), str(target), [(0, 120, 90)],
                                             cursor_size=30, cursor_img_path=str(self.asset), cursor_hotspot=self.hotspot)
        return target

    def test_full_render_with_unavailable_global_tmp_and_chinese_cwd(self):
        source = self.render_cursor()
        timeline = self.root / 'cursor.json'
        timeline.write_text(json.dumps([{'start': 0, 'end': 1, 'text': '中文字幕测试'}]), encoding='utf-8')
        target = self.root / 'final.mp4'
        subtitle_render.add_subtitles(str(source), str(target), style={'font_size': 31})
        info = json.loads(subprocess.check_output(['ffprobe', '-v', 'error', '-show_streams',
                                                   '-show_format', '-of', 'json', str(target)]))
        self.assertEqual({s['codec_type'] for s in info['streams']}, {'audio', 'video'})
        video = next(s for s in info['streams'] if s['codec_type'] == 'video')
        self.assertEqual((video['width'], video['height']), (640, 360))
        self.assertAlmostEqual(float(info['format']['duration']), 1, delta=0.15)
        cache = self.root / '.render-cache'
        self.assertEqual(len(list(cache.glob('cursor_overlay_*.png'))), 1)
        self.assertFalse(list(cache.glob('*.ffscript')))
        self.assertFalse(list(cache.glob('*.ass')))

    def test_rendered_single_and_two_lines_stay_inside_original_frame(self):
        bottoms = {}
        for name, text, font_size, bottom in (
                ('single', '字幕安全位置', 31, 6),
                ('two-lines', '字幕位置安全测试' * 4, 72, 1),
                ('raised', '字幕安全位置', 31, 15)):
            with self.subTest(name=name):
                timeline = self.root / 'captions.json'
                timeline.write_text(json.dumps([{'start': 0, 'end': 1, 'text': text}]), encoding='utf-8')
                target = self.root / (name + '.mp4')
                subtitle_render.add_subtitles(str(self.source), str(target), cursor_json_path=str(timeline),
                    style={'font_size': font_size, 'background_opacity': 0, 'margin_bottom': bottom, 'line_spacing': 2.4})
                pixels = subprocess.check_output(['ffmpeg', '-v', 'error', '-ss', '0.5', '-i', str(target),
                    '-frames:v', '1', '-f', 'image2pipe', '-vcodec', 'png', '-'])
                with Image.open(io.BytesIO(pixels)) as frame:
                    self.assertEqual(frame.size, (640, 360))
                    mask = Image.new('L', frame.size)
                    mask.putdata([255 if min(pixel[:3]) > 180 else 0 for pixel in frame.convert('RGB').get_flattened_data()])
                    box = mask.getbbox()
                self.assertIsNotNone(box, '实际解码画面必须有可见字幕')
                self.assertGreaterEqual(box[0], 10)
                self.assertLessEqual(box[2], 630)
                self.assertGreater(box[1], 180)
                self.assertLess(box[3], 360, '字幕不能贴边或截出视频画布')
                bottoms[name] = box[3]
        self.assertEqual(set(bottoms), {'single', 'two-lines', 'raised'})
        self.assertLess(bottoms['raised'], bottoms['single'], '增大底部边距必须让字幕在画面内上移')

    def test_failed_cursor_command_cleans_filter_and_preserves_source(self):
        original = self.source.read_bytes()
        cursor_render.filter_file_option()  # 缓存本机 FFmpeg 参数，之后只拦截合成命令。
        with patch.object(cursor_render.subprocess, 'run', side_effect=subprocess.CalledProcessError(1, 'ffmpeg')):
            with self.assertRaises(subprocess.CalledProcessError):
                self.render_cursor()
        self.assertEqual(self.source.read_bytes(), original)
        self.assertFalse(list((self.root / '.render-cache').glob('*.ffscript')))

    def test_failed_subtitle_command_cleans_ass_and_preserves_source(self):
        original = self.source.read_bytes()
        timeline = self.root / 'cursor.json'
        timeline.write_text(json.dumps([{'start': 0, 'end': 1, 'text': '字幕'}]), encoding='utf-8')
        with patch.object(subtitle_render, '_probe_resolution', return_value=(320, 180)), \
                patch.object(subtitle_render.subprocess, 'run', side_effect=OSError('模拟渲染进程启动失败')):
            with self.assertRaises(OSError):
                subtitle_render.add_subtitles(str(self.source), str(self.root / 'failed.mp4'))
        self.assertEqual(self.source.read_bytes(), original)
        self.assertFalse(list((self.root / '.render-cache').glob('*.ass')))


if __name__ == '__main__':
    unittest.main(verbosity=2)
