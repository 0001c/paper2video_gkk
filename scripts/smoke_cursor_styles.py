"""光标选项、配置、叠图缓存与真实视频验证，不调用模型/GPU。"""
import asyncio
import importlib.util
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from PIL import Image
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('cursor_web', ROOT / 'web/app.py')
web = importlib.util.module_from_spec(spec)
spec.loader.exec_module(web)
import cursor_styles
import cursor_render


def main():
    with tempfile.TemporaryDirectory(prefix='.cursor-smoke-', dir=ROOT) as work:
        root = Path(work)
        web.JOBS_ROOT = str(root / 'jobs')
        web.CONTENT_ROOT = str(root / 'content')
        web._QUEUE = asyncio.Queue()
        Path(web.JOBS_ROOT).mkdir()
        cursor_render.pv_env.TMP_DIR = str(root / 'overlays')
        client = TestClient(web.app, client=('127.0.0.1', 50000))
        choices = client.get('/api/options').json()['cursors']
        assert len(choices) == 8
        originals = {}
        overlays = []
        for choice in choices:
            asset, anchor = cursor_styles.resolve(choice['value'])
            if asset is None:
                continue
            originals[asset] = asset.read_bytes()
            assert client.get(choice['image_url']).status_code == 200
            with Image.open(asset) as image:
                assert image.convert('RGBA').getextrema()[3][0] == 0
            scaled = cursor_render.cursor_overlay(asset, 48)
            assert cursor_render.cursor_overlay(asset, 48) == scaled
            overlays.append(scaled)
        assert len(set(overlays)) == 7, '同尺寸不同素材不能混用缓存'
        assert client.get('/api/cursors/unknown.png').status_code == 404
        for bad in ({'cursor_style': '../red'}, {'cursor_scale': 0}, {'cursor_scale': True}):
            try:
                web._normalize_models(bad)
                raise AssertionError('非法光标设置未拒绝')
            except web.HTTPException as exc:
                assert exc.status_code == 422
        ingest = Path(web.CONTENT_ROOT, 'a' * 12, 'ingest')
        ingest.mkdir(parents=True)
        (ingest / 'paper_text.md').write_text('虚构课程材料', encoding='utf-8')
        created = client.post('/api/jobs', json={'ingest_id': 'content_' + 'a' * 12,
            'config': {'slide_lang': 'zh', 'script_lang': 'zh', 'cursor_style': 'ring-gold', 'cursor_scale': 1.5}})
        assert created.status_code == 200, created.text
        job = web._load_job(created.json()['job_id'])
        argv = web._build_argv(job)
        assert argv[argv.index('--cursor_style') + 1] == 'ring-gold'
        assert argv[argv.index('--cursor_scale') + 1] == '1.5'
        facts = web._artifact_facts(job)
        # 光标只影响最终画面，不能使已付费的课件讲稿缓存失效。
        changed = {**job, 'config': {**job['config'], 'cursor_style': 'arrow-white'}}
        assert web.artifact_cache.deck_key(facts) == web.artifact_cache.deck_key(web._artifact_facts(changed))
        job['status'] = 'failed'
        web._save_job(job)
        resumed = client.post('/api/jobs/' + job['id'] + '/rerun', json={'reuse_script': True, 'cursor_style': 'arrow-white', 'cursor_scale': 0.75})
        assert resumed.status_code == 409 and '幻灯片尚未完整生成' in resumed.json()['detail'], resumed.text
        if '--media' in sys.argv:
            source = root / 'source.mp4'
            subprocess.run(['ffmpeg', '-y', '-v', 'error', '-f', 'lavfi', '-i', 'color=c=0x303846:s=320x180:r=10:d=1',
                '-f', 'lavfi', '-i', 'sine=frequency=440:duration=1', '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-c:a', 'aac', str(source)], check=True)
            def bounds(video):
                data = subprocess.check_output(['ffmpeg', '-v', 'error', '-ss', '0.8', '-i', str(video), '-frames:v', '1', '-f', 'image2pipe', '-vcodec', 'png', '-'])
                with Image.open(io.BytesIO(data)) as frame:
                    pixels = frame.convert('RGB')
                    base = pixels.getpixel((0, 0))
                    mask = Image.new('L', pixels.size)
                    mask.putdata([255 if max(abs(a - b) for a, b in zip(pixel, base)) > 28 else 0 for pixel in pixels.get_flattened_data()])
                    return mask.getbbox()
            for style in cursor_styles.STYLES:
                asset, anchor = cursor_styles.resolve(style)
                target = root / (style + '.mp4')
                cursor_render.render_cursor_on_video(str(source), str(target), [(0, 120, 90)], cursor_size=48,
                    cursor_img_path=str(asset) if asset else None, cursor_hotspot=anchor)
                audio = subprocess.check_output(['ffprobe', '-v', 'error', '-select_streams', 'a', '-show_entries', 'stream=codec_name', '-of', 'csv=p=0', str(target)])
                assert audio.strip() == b'aac', '光标合成不能丢失已有音频'
                if asset:
                    box = bounds(target)
                    assert box and box[0] <= 122 and box[2] >= 118 and box[1] <= 92 and box[3] >= 88, (style, box)
                else:
                    assert source.read_bytes() == target.read_bytes()
            last = root / 'last.mp4'
            asset, anchor = cursor_styles.resolve('laser-red')
            cursor_render.render_cursor_on_video(str(source), str(last), [(0, 80, 90), (0.3, 220, 90)], cursor_size=48, cursor_img_path=str(asset))
            box = bounds(last)
            assert box and abs((box[0] + box[2]) / 2 - 220) < 5, '最后一个目标必须保持到视频结束'
            long_video = root / 'long-targets.mp4'
            cursor_render.render_cursor_on_video(str(source), str(long_video), [(i / 1000, 100 + i % 2 * 120, 90) for i in range(1000)], cursor_size=48, cursor_img_path=str(asset))
            assert long_video.is_file() and not list((root / '.render-cache').glob('*.ffscript')), '长滤镜不能超过命令行限制，临时脚本应清理'
        assert all(asset.read_bytes() == data for asset, data in originals.items()), '不能改写仓库源素材'
    print('PASS 光标选项、透明素材、配置透传、缓存隔离、付费产物复用' + ('、真实视频热点与末目标保持' if '--media' in sys.argv else ''))


if __name__ == '__main__':
    main()
