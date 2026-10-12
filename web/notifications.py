"""Bark 任务通知：地址按 Windows 账户加密，只发送任务状态。"""
import hashlib
import ipaddress
import json
import re
from urllib.parse import unquote, urlsplit, urlunsplit

from pydantic import SecretStr, field_validator
import requests

from web.credential_storage import CredentialFile
from web.model_config import StrictModel


def destination(value):
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= 2048:
        raise ValueError('push_url：请填写 Bark 的完整推送地址')
    parsed = urlsplit(value.strip())
    try:
        port = parsed.port
    except ValueError:
        raise ValueError('push_url：端口无效') from None
    if parsed.scheme not in ('https', 'http') or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError('push_url：需要无账号、查询参数及片段的 HTTP(S) 推送地址')
    host = parsed.hostname
    if parsed.scheme == 'http' and host != 'localhost':
        try:
            local = ipaddress.ip_address(host)
        except ValueError:
            raise ValueError('push_url：云端 Bark 必须使用 HTTPS；HTTP 仅支持本机或私网 IP') from None
        if not (local.is_loopback or local.is_private) or local.is_link_local or local.is_unspecified or local.is_multicast:
            raise ValueError('push_url：HTTP 地址必须是本机或私网 IP')
    prefix, _, key = parsed.path.rstrip('/').rpartition('/')
    key = unquote(key)
    if not re.fullmatch(r'[A-Za-z0-9_-]{8,256}', key):
        raise ValueError('push_url：地址末尾应为 Bark 设备 Key')
    authority = '[' + host + ']' if ':' in host else host
    if port:
        authority += ':' + str(port)
    server = urlunsplit((parsed.scheme, authority, prefix, '', ''))
    return server, key


class BarkUpdate(StrictModel):
    enabled: bool = False
    push_url: SecretStr | None = None
    completed: bool = True
    failed: bool = True
    review: bool = True

    @field_validator('push_url')
    @classmethod
    def valid_url(cls, value):
        if value:
            destination(value.get_secret_value())
        return value


def job_event(job):
    if job.get('qa_next_job_id') or job.get('status') == 'canceled':
        return None
    if job.get('status') == 'failed' or job.get('qa_error'):
        return 'failed'
    if job.get('status') == 'done':
        return 'completed'
    if job.get('status') == 'awaiting_review':
        return 'review'
    return None


def event_fingerprint(job, event):
    material = {'event': event, 'status': job.get('status'), 'phase': job.get('phase'),
                'error': job.get('qa_error') or job.get('error'),
                'reports': {stage: report.get('fingerprint') for stage, report in (job.get('evaluation') or {}).items()}}
    return hashlib.sha256(json.dumps(material, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


class BarkStore:
    def __init__(self, path):
        self.storage = CredentialFile(path)
        self.config = BarkUpdate()
        self.error = None

    def restore(self):
        try:
            records = self.storage.load()
            if len(records) > 1:
                raise ValueError('记录数量异常')
            self.config = BarkUpdate.model_validate(records[0]) if records else BarkUpdate()
            self.error = None
        except (OSError, ValueError):
            self.error = 'Bark 本机配置无法恢复，请检查当前 Windows 账户或重新保存配置'

    def public(self):
        return {**self.config.model_dump(exclude={'push_url'}), 'configured': self.config.push_url is not None,
                'server': destination(self.config.push_url.get_secret_value())[0] if self.config.push_url else '', 'error': self.error}

    def update(self, payload):
        values = payload.model_dump()
        values['push_url'] = payload.push_url or self.config.push_url
        candidate = BarkUpdate.model_validate(values)
        if candidate.enabled and not candidate.push_url:
            raise ValueError('push_url：开启通知前请填写 Bark 推送地址')
        record = candidate.model_dump(exclude={'push_url'})
        if candidate.push_url:
            record['push_url'] = candidate.push_url.get_secret_value()
        self.storage.save([record])
        self.config, self.error = candidate, None
        return self.public()

    def send(self, title, body):
        if not self.config.push_url:
            raise ValueError('请先保存 Bark 推送地址')
        server, key = destination(self.config.push_url.get_secret_value())
        try:
            with requests.Session() as session:
                session.trust_env = False
                response = session.post(server + '/push', json={'device_key': key, 'title': title, 'body': body,
                    'group': 'Paper2Video', 'isArchive': '1'}, timeout=(3, 5), allow_redirects=False)
                result = response.json() if response.status_code == 200 else None
                if not isinstance(result, dict) or result.get('code') != 200:
                    raise ValueError('Bark 服务未确认接收，请检查推送地址和设备 Key')
        except (requests.RequestException, ValueError):
            # 异常原文可能含设备 Key，不能写入日志/HTTP响应。
            raise ValueError('Bark 推送失败，请检查地址、设备 Key 或网络；任务产物已保留') from None

    def enabled_for(self, event):
        return self.config.enabled and self.config.push_url is not None and not self.error and getattr(self.config, event)

    def send_job(self, job, event):
        titles = {'completed': '讲解视频已完成', 'failed': '讲解任务需要处理', 'review': '讲解任务等待审核'}
        messages = {'completed': '成片已生成，可在任务详情查看。', 'failed': '生成或评价未完成，请打开任务详情查看原因。',
                    'review': '已完成当前阶段，请检查课件、讲稿或评估结果。'}
        self.send(titles[event], f"任务 {job['id']}\n{messages[event]}")
