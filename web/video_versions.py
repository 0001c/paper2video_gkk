"""成片不可变快照。复制与校验成功后才发布版本，失败时禁止覆盖原成片。"""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import shutil
import uuid


def sha(path):
    value = hashlib.sha256()
    with Path(path).open('rb') as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b''):
            value.update(block)
    return value.hexdigest()


def owner(job):
    identity = job.get('video_versions_owner') or job['id']
    if not re.fullmatch(r'[a-zA-Z0-9_-]+', identity):
        raise ValueError('成片版本所属任务无效')
    return identity


def directory(job, jobs_root):
    return Path(jobs_root) / owner(job) / 'video_versions'


def archive(job, jobs_root, result_dir, reason):
    root, video = Path(result_dir), Path(result_dir) / '3_merage.mp4'
    if not video.is_file():
        return None
    script = root / 'subtitle_w_cursor.txt'
    metadata = {'job_id': job['id'], 'video_sha256': sha(video), 'evaluation': job.get('evaluation') or {},
                'config': job.get('config') or {}, 'script_sha256': sha(script) if script.is_file() else None}
    identity = hashlib.sha256(json.dumps(metadata, ensure_ascii=False, sort_keys=True).encode('utf-8')).hexdigest()
    versions = directory(job, jobs_root)
    versions.mkdir(parents=True, exist_ok=True)
    for manifest in versions.glob('*/manifest.json'):
        saved = json.loads(manifest.read_text(encoding='utf-8'))
        if saved.get('identity') == identity:
            if sha(manifest.parent / 'video.mp4') != metadata['video_sha256']:
                raise ValueError('既有成片备份校验失败，已停止修复')
            return saved['id']
    timestamp = datetime.now(timezone.utc).isoformat()
    version_id = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S') + '-' + uuid.uuid4().hex[:12]
    temporary, target = versions / ('.pending-' + version_id), versions / version_id
    temporary.mkdir()
    try:
        shutil.copy2(video, temporary / 'video.mp4')
        if sha(temporary / 'video.mp4') != metadata['video_sha256'] or sha(video) != metadata['video_sha256']:
            raise ValueError('成片在备份期间发生变化，已停止修复')
        if script.is_file():
            shutil.copy2(script, temporary / 'script.txt')
            if sha(temporary / 'script.txt') != metadata['script_sha256']:
                raise ValueError('讲稿在备份期间发生变化，已停止修复')
        saved = {**metadata, 'id': version_id, 'identity': identity, 'created_at': timestamp, 'reason': reason,
                 'size_bytes': (temporary / 'video.mp4').stat().st_size}
        (temporary / 'manifest.json').write_text(json.dumps(saved, ensure_ascii=False, indent=2), encoding='utf-8')
        temporary.rename(target)
    except Exception:
        # 只清理由本次创建且已确认在版本目录内的未发布文件。
        if temporary.resolve().parent == versions.resolve() and temporary.exists():
            shutil.rmtree(temporary)
        raise
    return version_id


def list_versions(job, jobs_root):
    result = []
    for manifest in directory(job, jobs_root).glob('*/manifest.json'):
        saved = json.loads(manifest.read_text(encoding='utf-8'))
        if not (manifest.parent / 'video.mp4').is_file():
            continue
        result.append({key: saved.get(key) for key in ('id', 'created_at', 'job_id', 'reason', 'size_bytes', 'video_sha256')})
    return sorted(result, key=lambda row: row['created_at'], reverse=True)


def version_file(job, jobs_root, version_id, kind):
    if not re.fullmatch(r'\d{8}T\d{6}-[a-f0-9]{12}', version_id):
        raise ValueError('成片版本编号无效')
    filename = {'video': 'video.mp4', 'report': 'manifest.json', 'script': 'script.txt'}.get(kind)
    if not filename:
        raise ValueError('成片版本文件类型无效')
    root = directory(job, jobs_root).resolve()
    target = (root / version_id / filename).resolve()
    if not target.is_relative_to(root) or not target.is_file():
        raise FileNotFoundError('成片备份文件不存在')
    return target
