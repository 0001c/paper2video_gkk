"""真实小视频回归：全局临时目录不可用时，光标与字幕仍使用任务产物目录。"""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

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
        subprocess.run(['ffmpeg', '-y', '-v', 'error', '-f', 'lavfi', '-i', 'color=c=blue:s=320x180:r=10:d=1',
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
        self.assertGreater(video['height'], 180)
        self.assertAlmostEqual(float(info['format']['duration']), 1, delta=0.15)
        cache = self.root / '.render-cache'
        self.assertEqual(len(list(cache.glob('cursor_overlay_*.png'))), 1)
        self.assertFalse(list(cache.glob('*.ffscript')))
        self.assertFalse(list(cache.glob('*.ass')))

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
