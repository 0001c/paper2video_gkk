"""Wan 原生数字人协议；持久化远程 ID，所有 POST 均不自动重试。"""

import json
import math
import os
import re
import subprocess
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import httpx
from PIL import Image

from avatar_segments import verify_segment
from bailian_speech import atomic_json, digest, native_base
from web import model_costs


MODEL = 'wan2.2-s2v'
DETECT_MODEL = MODEL + '-detect'
UPLOAD_POLICY_URL = 'https://dashscope.aliyuncs.com/api/v1/uploads'
MAX_AUDIO_BYTES = 15 * 1024 * 1024
MAX_VIDEO_BYTES = 512 * 1024 * 1024
STATUSES = frozenset(('PENDING', 'RUNNING', 'SUCCEEDED', 'FAILED', 'UNKNOWN', 'CANCELED'))
ID_PATTERN = re.compile(r'[A-Za-z0-9_-]{1,128}\Z')
OSS_PATTERN = re.compile(r'dashscope-(?:file|result)(?:-[a-z0-9-]+)?\.oss-cn-(?:beijing|hangzhou|shanghai)\.aliyuncs\.com\Z')


def now():
    return datetime.now(timezone.utc).isoformat()


def safe_id(value):
    return value if isinstance(value, str) and ID_PATTERN.fullmatch(value) else None


def object_field(response, name):
    value = response.get(name)
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError('百炼响应结构无效')
    return value


def read_record(filename):
    target = Path(filename)
    if target.stat().st_size > 1024 * 1024:
        raise ValueError('Wan 任务记录超过大小限制')
    value = json.loads(target.read_text(encoding='utf-8'))
    if not isinstance(value, dict):
        raise ValueError('Wan 任务记录结构无效')
    return value


def oss_url(value, uploading=False):
    """只允许百炼专属 OSS 桶；官方返回的 http 结果升级为 https。"""
    if not isinstance(value, str) or len(value) > 16384 or any(ord(char) < 33 for char in value):
        raise ValueError('百炼媒体地址无效')
    parsed = urlsplit(value)
    host = parsed.hostname or ''
    try:
        port = parsed.port
    except ValueError:
        raise ValueError('百炼媒体地址端口无效') from None
    prefix = 'dashscope-file' if uploading else 'dashscope-result'
    if (not OSS_PATTERN.fullmatch(host) or not host.startswith(prefix) or parsed.scheme not in ('http', 'https')
            or parsed.username is not None or parsed.password is not None or port not in (None, 443)
            or parsed.fragment or (uploading and (parsed.scheme != 'https' or parsed.query or parsed.path not in ('', '/')))):
        raise ValueError('百炼媒体地址不在受信 HTTPS OSS 范围内')
    return urlunsplit(('https', host, parsed.path, parsed.query, ''))


@contextmanager
def record_lock(filename):
    """跨服务/worker 的操作锁；进程退出时由操作系统释放。"""
    target = Path(filename)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.with_suffix(target.suffix + '.lock').open('a+b') as handle:
        try:
            if os.fstat(handle.fileno()).st_size == 0:
                handle.write(b'0')
                handle.flush()
            handle.seek(0)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise ValueError('此 Wan 片段正在执行；停止当前 worker 后再对账或取消') from None
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == 'nt':
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class AvatarClient:
    def __init__(self, connection, api_key, client=None, usage_directory=None):
        self.base = native_base(connection)
        self.scope_id = connection.get('scope_id')
        if not isinstance(self.scope_id, str) or not self.scope_id or not api_key:
            raise ValueError('Wan 需要连接范围与已绑定的凭据')
        self.api_key = api_key
        self.client = client
        self.usage_directory = usage_directory or os.environ.get('PAPER2VIDEO_USAGE_DIR')

    @contextmanager
    def _client(self):
        client = self.client or httpx.Client(timeout=httpx.Timeout(60, connect=20), follow_redirects=False, trust_env=False)
        try:
            yield client
        finally:
            if self.client is None:
                client.close()

    def _request(self, method, url, *, authorized=True, limit=1024 * 1024, **kwargs):
        headers = {'Authorization': 'Bearer ' + self.api_key} if authorized else {}
        headers.update(kwargs.pop('headers', {}))
        with self._client() as client:
            request = client.build_request(method, url, headers=headers, **kwargs)
            if not authorized:
                for name in ('authorization', 'cookie', 'proxy-authorization'):
                    if name in request.headers:
                        del request.headers[name]
            response = client.send(request, stream=True, follow_redirects=False, auth=None)
            try:
                body = bytearray()
                for chunk in response.iter_bytes():
                    body.extend(chunk)
                    if len(body) > limit:
                        raise ValueError('百炼响应超过大小限制')
                return response.status_code, bytes(body)
            finally:
                response.close()

    def _json(self, method, url, **kwargs):
        status, body = self._request(method, url, **kwargs)
        if status != 200:
            raise ValueError('百炼请求失败（HTTP {}）；未自动重试'.format(status))
        try:
            data = json.loads(body)
        except (ValueError, UnicodeDecodeError):
            raise ValueError('百炼 JSON 响应无效；受理状态可能未知') from None
        if not isinstance(data, dict) or data.get('code'):
            raise ValueError('百炼返回错误；未自动重试')
        return data

    def _save(self, filename, record, **changes):
        record.update(changes, updated_at=now())
        atomic_json(filename, record)
        return record

    def _ticket(self, record):
        identity = record.get('usage_record_id')
        if not identity:
            return None
        if not self.usage_directory:
            raise ValueError('恢复 Wan 费用记录需要原费用账本目录')
        ticket = model_costs.restore(self.usage_directory, identity)
        if ticket[1].get('model') != record.get('model') or ticket[1].get('role') != 'avatar':
            raise ValueError('Wan 费用记录与远程任务不匹配')
        return ticket

    def _begin(self, record, model):
        ticket = model_costs.begin('avatar', model, self.base, self.usage_directory)
        record['usage_record_id'] = ticket[1]['id'] if ticket else None
        return ticket

    def _finish(self, record, usage=None, state='succeeded'):
        ticket = self._ticket(record)
        if ticket and ticket[1].get('state') == 'succeeded':
            # 已知估算保留当次价格；续查缺失用量或本地取消不能抹掉已发生费用。
            if state != 'succeeded' or ticket[1].get('estimated_cny') is not None:
                return
            usage = {**model_costs.clean_usage(ticket[1].get('usage')), **model_costs.clean_usage(usage)}
        model_costs.finish(ticket, usage, state=state)

    def _load(self, filename):
        record = read_record(filename)
        if (record.get('schema_version') != 1 or record.get('provider') != 'bailian'
                or record.get('model') != MODEL or record.get('scope_id') != self.scope_id or record.get('endpoint') != self.base):
            raise ValueError('Wan 远程任务与当前模型/连接范围不一致')
        if record.get('task_id') is not None and not safe_id(record['task_id']):
            raise ValueError('Wan 远程任务 ID 无效')
        if not isinstance(record.get('output_path'), str):
            raise ValueError('Wan 产物归档路径无效')
        target = Path(record['output_path']).resolve()
        if target.parent != Path(filename).resolve().parent or target.suffix != '.mp4':
            raise ValueError('Wan 产物必须归档到片段任务记录目录')
        return record

    def _upload(self, path, model, cancelled):
        if cancelled():
            raise InterruptedError('Wan 已停止上传与新提交')
        # 官方政策入口固定；workspace 原生地址不能推导上传接口。
        policy = self._json('GET', UPLOAD_POLICY_URL, params={'action': 'getPolicy', 'model': model}).get('data')
        names = {'OSSAccessKeyId': 'oss_access_key_id', 'Signature': 'signature', 'policy': 'policy',
                 'x-oss-object-acl': 'x_oss_object_acl', 'x-oss-forbid-overwrite': 'x_oss_forbid_overwrite'}
        if not isinstance(policy, dict) or any(not isinstance(policy.get(name), str) or not policy[name] for name in names.values()):
            raise ValueError('百炼临时上传凭证无效')
        directory = policy.get('upload_dir')
        if (not isinstance(directory, str) or not re.fullmatch(r'dashscope-instant/[A-Za-z0-9/_-]+', directory)
                or '..' in directory or policy['x_oss_object_acl'] != 'private' or policy['x_oss_forbid_overwrite'] != 'true'):
            raise ValueError('百炼临时上传目录/私有策略无效')
        host = oss_url(policy.get('upload_host'), uploading=True)
        key = directory.rstrip('/') + '/' + uuid.uuid4().hex + Path(path).suffix.lower()
        fields = {name: policy[source] for name, source in names.items()}
        fields.update(key=key, success_action_status='200')
        maximum = policy.get('max_file_size_mb')
        if type(maximum) not in (int, float) or not 0 < maximum <= 1024 or Path(path).stat().st_size > maximum * 1024 * 1024:
            raise ValueError('素材超过临时上传凭证大小限制')
        if cancelled():
            raise InterruptedError('Wan 已停止上传与新提交')
        with Path(path).open('rb') as handle:
            status, _ = self._request('POST', host, authorized=False, data=fields,
                                      files={'file': (Path(key).name, handle, 'application/octet-stream')})
        if status != 200:
            raise ValueError('百炼临时上传失败；未自动重试')
        # 上传签名和模型绑定资源 URL 仅存在本次调用内存，重启后重新申请。
        return 'oss://' + key

    def _detect(self, reference, filename, reference_sha, cancelled):
        fingerprint = digest({'reference_sha256': reference_sha, 'model': DETECT_MODEL,
                              'target_model': MODEL, 'scope_id': self.scope_id, 'endpoint': self.base})
        with record_lock(filename):
            if Path(filename).exists():
                record = read_record(filename)
                if record.get('fingerprint') != fingerprint:
                    raise ValueError('Wan 检测记录与肖像/模型/连接范围不一致')
                if record.get('state') == 'succeeded':
                    self._finish(record, record.get('usage', {}))
                    if record.get('check_pass') is True:
                        return
                    raise ValueError('肖像未通过 Wan 图像检测；本次检测已按图片计费')
                raise ValueError('Wan 图像检测未通过或受理状态未知；禁止自动重复计费检测')
            image_url = self._upload(reference, DETECT_MODEL, cancelled)
            if cancelled():
                raise InterruptedError('Wan 已停止新提交')
            record = {'schema_version': 1, 'provider': 'bailian', 'model': DETECT_MODEL, 'target_model': MODEL,
                      'fingerprint': fingerprint, 'scope_id': self.scope_id, 'endpoint': self.base,
                      'reference_sha256': reference_sha, 'state': 'submitting', 'submitted_at': now()}
            ticket = self._begin(record, DETECT_MODEL)
            self._save(filename, record)
            try:
                response = self._json('POST', self.base + '/services/aigc/image2video/face-detect',
                                      headers={'X-DashScope-OssResourceResolve': 'enable'},
                                      json={'model': DETECT_MODEL, 'input': {'image_url': image_url}})
                output = object_field(response, 'output')
                if type(output.get('check_pass')) is not bool:
                    raise ValueError('Wan 图像检测结果不完整')
                # 本接口每次仅传入一图；成功的检测请求（含未通过）都按一张计费。
                clean = {'images': 1}
                self._save(filename, record, state='succeeded', check_pass=output['check_pass'],
                           request_id=safe_id(response.get('request_id')), usage=clean)
                model_costs.finish(ticket, clean)
            except (httpx.HTTPError, ValueError):
                if record['state'] != 'succeeded':
                    self._save(filename, record, state='submission_unknown')
                raise ValueError('Wan 图像检测失败或受理状态未知；禁止自动重提') from None
            if not record['check_pass']:
                raise ValueError('肖像未通过 Wan 图像检测；本次检测已按图片计费')

    def _download(self, url, output):
        url = oss_url(url)
        target = Path(output)
        temporary = target.with_name(target.name + '.' + uuid.uuid4().hex + '.tmp')
        try:
            with self._client() as client:
                request = client.build_request('GET', url)
                for name in ('authorization', 'cookie', 'proxy-authorization'):
                    if name in request.headers:
                        del request.headers[name]
                response = client.send(request, stream=True, follow_redirects=False, auth=None)
                try:
                    if response.status_code != 200:
                        raise ValueError('Wan 视频下载失败；保留远程 ID 可继续对账')
                    size = 0
                    deadline = time.monotonic() + 180
                    with temporary.open('wb') as handle:
                        for chunk in response.iter_bytes():
                            size += len(chunk)
                            if size > MAX_VIDEO_BYTES or time.monotonic() > deadline:
                                raise ValueError('Wan 视频下载超过大小/时间限制')
                            handle.write(chunk)
                    with temporary.open('rb') as handle:
                        header = handle.read(16)
                    if size < 16 or header[4:8] != b'ftyp':
                        raise ValueError('Wan 下载内容不是 MP4 视频')
                    probe = subprocess.run(['ffprobe', '-v', 'error', '-select_streams', 'v:0',
                                            '-show_entries', 'stream=codec_type:format=duration', '-of', 'json', str(temporary)],
                                           capture_output=True, check=True, timeout=20)
                    metadata = json.loads(probe.stdout)
                    duration = float(metadata.get('format', {}).get('duration', 0))
                    if not metadata.get('streams') or not math.isfinite(duration) or duration <= 0:
                        raise ValueError('Wan 下载视频缺少可解码的视频流/时长')
                    temporary.replace(target)
                finally:
                    response.close()
        finally:
            temporary.unlink(missing_ok=True)
        return digest(target.read_bytes())

    def _reconcile(self, filename, record):
        task_id = record.get('task_id')
        if not task_id:
            raise ValueError('Wan 未保存远程 ID；必须人工核对，禁止自动提交')
        try:
            response = self._json('GET', self.base + '/tasks/' + task_id)
            output = object_field(response, 'output')
            if (output.get('task_id') != task_id or not isinstance(output.get('task_status'), str)
                    or output['task_status'] not in STATUSES):
                raise ValueError('Wan 查询返回的任务 ID/状态无效')
        except (httpx.HTTPError, ValueError):
            self._save(filename, record, state='query_failed', possible_billing=True)
            raise ValueError('Wan 续查失败；保留远程 ID，禁止自动重提') from None
        status = output['task_status']
        state = {'PENDING': 'pending', 'RUNNING': 'running', 'SUCCEEDED': 'succeeded',
                 'FAILED': 'failed', 'UNKNOWN': 'remote_unknown', 'CANCELED': 'canceled'}[status]
        self._save(filename, record, state=state, remote_status=status, query_request_id=safe_id(response.get('request_id')),
                   possible_billing=status not in ('FAILED', 'CANCELED'), cancellation_confirmed=status == 'CANCELED')
        if status == 'SUCCEEDED':
            usage = object_field(response, 'usage')
            clean = {**model_costs.clean_usage(record.get('usage')),
                     **model_costs.clean_usage({'duration_seconds': usage.get('duration'), 'resolution': usage.get('SR')})}
            self._save(filename, record, usage=clean)
            self._finish(record, clean)
            target = Path(record['output_path'])
            if target.exists() and record.get('output_sha256') == digest(target.read_bytes()):
                return record
            try:
                video_url = object_field(output, 'results').get('video_url')
                sha = self._download(video_url, target)
                self._save(filename, record, output_sha256=sha, archived_at=now())
            except (httpx.HTTPError, OSError, ValueError, subprocess.SubprocessError):
                self._save(filename, record, state='download_failed')
                raise ValueError('Wan 已成功生成/计费但归档失败；保留远程 ID 可续查下载') from None
        elif status in ('FAILED', 'CANCELED'):
            self._finish(record, state=state)
        return record

    def reconcile(self, record_path):
        """一次查询并归档；绝不上传或提交生成任务。"""
        with record_lock(record_path):
            record = self._load(record_path)
            target = Path(record['output_path'])
            if record.get('state') == 'succeeded' and target.exists() and record.get('output_sha256') == digest(target.read_bytes()):
                self._finish(record, record.get('usage', {}))
                return record
            return self._reconcile(record_path, record)

    def _cancel(self, filename, record):
        self._save(filename, record, cancellation_requested=True)
        target = Path(record['output_path'])
        if record.get('state') == 'succeeded' and target.exists() and record.get('output_sha256') == digest(target.read_bytes()):
            self._finish(record, record.get('usage', {}))
            return record
        if not record.get('task_id'):
            unknown = record.get('state') in ('submitting', 'submission_unknown')
            return self._save(filename, record, state='submission_unknown' if unknown else 'cancelled_before_submission',
                              possible_billing=unknown, cancellation_confirmed=not unknown)
        try:
            self._reconcile(filename, record)
            if record.get('remote_status') in ('SUCCEEDED', 'FAILED', 'CANCELED'):
                return record
            if record.get('remote_status') == 'PENDING' and not record.get('cancel_post_attempted'):
                # 先记尝试，宕机/超时后不重复 POST；随后只以 GET 确认。
                self._save(filename, record, cancel_post_attempted=True, state='cancel_unconfirmed')
                response = self._json('POST', self.base + '/tasks/' + record['task_id'] + '/cancel')
                self._save(filename, record, cancel_request_id=safe_id(response.get('request_id')))
                self._reconcile(filename, record)
        except (httpx.HTTPError, ValueError):
            pass
        if record.get('remote_status') not in ('SUCCEEDED', 'FAILED', 'CANCELED'):
            self._save(filename, record, state='cancel_unconfirmed', cancellation_confirmed=False, possible_billing=True)
        return record

    def cancel(self, record_path):
        with record_lock(record_path):
            return self._cancel(record_path, self._load(record_path))

    def run_segment(self, reference, audio, record_path, output_path, model=MODEL, resolution='480P',
                    scope_id=None, cancelled=lambda: False, detection_path=None, poll_interval=15, timeout=1800):
        if model != MODEL or resolution not in ('480P', '720P') or (scope_id is not None and scope_id != self.scope_id):
            raise ValueError('Wan 模型/分辨率/连接范围无效')
        if (type(poll_interval) not in (int, float) or not 0 < poll_interval <= 3600
                or type(timeout) not in (int, float) or not 0 < timeout <= 86400):
            raise ValueError('Wan 轮询间隔/超时无效')
        filename = Path(record_path).resolve()
        target = Path(output_path).resolve()
        if target.parent != filename.parent or target.suffix != '.mp4':
            raise ValueError('Wan 产物必须归档到片段任务记录目录')
        with record_lock(filename):
            if Path(audio).stat().st_size >= MAX_AUDIO_BYTES:
                raise ValueError('Wan 单段音频文件必须小于 15 MB')
            seconds = verify_segment(audio)
            if Path(reference).suffix.lower() not in ('.jpg', '.jpeg', '.png', '.bmp', '.webp') or Path(reference).stat().st_size > 20 * 1024 * 1024:
                raise ValueError('Wan 肖像格式/大小无效')
            with Image.open(reference) as image:
                if image.format not in ('JPEG', 'PNG', 'BMP', 'WEBP') or getattr(image, 'n_frames', 1) != 1:
                    raise ValueError('Wan 肖像必须为受支持格式的单帧静态图')
                if not all(400 <= dimension <= 7000 for dimension in image.size):
                    raise ValueError('Wan 肖像宽高必须为 400 至 7000 像素')
                image.verify()
            reference_sha, audio_sha = digest(Path(reference).read_bytes()), digest(Path(audio).read_bytes())
            identity = {'model': MODEL, 'scope_id': self.scope_id, 'endpoint': self.base,
                        'parameters': {'resolution': resolution}, 'reference_sha256': reference_sha, 'audio_sha256': audio_sha}
            fingerprint = digest(identity)
            if filename.exists():
                record = self._load(filename)
                if record.get('fingerprint') != fingerprint or record['output_path'] != str(target):
                    raise ValueError('Wan 片段指纹已变化；保留原远程记录并使用新派生目录')
                if record.get('state') == 'succeeded' and target.exists() and record.get('output_sha256') == digest(target.read_bytes()):
                    self._finish(record, record.get('usage', {}))
                    return record
                if not record.get('task_id') and record.get('state') != 'prepared':
                    raise ValueError('Wan 受理状态未知或此前已停止；禁止自动重提')
            else:
                record = {'schema_version': 1, 'provider': 'bailian', **identity, 'fingerprint': fingerprint,
                          'state': 'prepared', 'task_id': None, 'request_id': None, 'output_path': str(target),
                          'input_duration_seconds': seconds, 'created_at': now(), 'possible_billing': False}
                self._save(filename, record)
            if cancelled():
                return self._cancel(filename, record)
            if not record.get('task_id'):
                try:
                    detection_path = detection_path or filename.parent / ('wan-detection-' + reference_sha + '.json')
                    self._detect(reference, detection_path, reference_sha, cancelled)
                    image_url = self._upload(reference, MODEL, cancelled)
                    audio_url = self._upload(audio, MODEL, cancelled)
                    if cancelled():
                        raise InterruptedError('Wan 已停止新提交')
                    self._begin(record, MODEL)
                    self._save(filename, record, state='submitting', submitted_at=now(), possible_billing=True)
                    response = self._json('POST', self.base + '/services/aigc/image2video/video-synthesis',
                                          headers={'X-DashScope-Async': 'enable', 'X-DashScope-OssResourceResolve': 'enable'},
                                          json={'model': MODEL, 'input': {'image_url': image_url, 'audio_url': audio_url},
                                                'parameters': {'resolution': resolution}})
                    output = object_field(response, 'output')
                    task_id = safe_id(output.get('task_id'))
                    if not task_id:
                        raise ValueError('Wan 提交缺少有效远程 ID')
                    status = output.get('task_status')
                    self._save(filename, record, task_id=task_id, request_id=safe_id(response.get('request_id')),
                               state='pending', remote_status=status if isinstance(status, str) and status in STATUSES else 'PENDING')
                except InterruptedError:
                    return self._cancel(filename, record)
                except (httpx.HTTPError, ValueError):
                    if record['state'] == 'submitting':
                        self._save(filename, record, state='submission_unknown', possible_billing=True)
                    raise ValueError('Wan 上传/检测/提交失败；请核对持久化状态，未自动重试') from None
            deadline = time.monotonic() + timeout
            while True:
                if cancelled():
                    return self._cancel(filename, record)
                self._reconcile(filename, record)
                if record['state'] == 'succeeded':
                    return record
                if record['state'] in ('failed', 'canceled', 'remote_unknown'):
                    raise ValueError('Wan 远程任务已失败、取消或无法查询；保留 ID，禁止自动重提')
                if time.monotonic() >= deadline:
                    self._save(filename, record, state='poll_timeout', possible_billing=True)
                    raise ValueError('Wan 轮询超时；云端可能继续处理/计费，可按远程 ID 恢复')
                until = min(time.monotonic() + poll_interval, deadline)
                while time.monotonic() < until and not cancelled():
                    time.sleep(min(0.5, max(0, until - time.monotonic())))
