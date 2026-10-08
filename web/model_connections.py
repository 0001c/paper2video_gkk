"""受信模型连接；秘密只保存于服务进程内存。"""

import copy
import ipaddress
import os
import uuid
from urllib.parse import urlsplit, urlunsplit
from typing import Literal

from pydantic import Field, SecretStr, field_validator

from web.model_config import ConfigurationError, ConnectionID, StrictModel


class ConnectionCreate(StrictModel):
    label: str = Field(min_length=1, max_length=80)
    provider: Literal['bailian', 'openai_compatible']
    compatible_base_url: str = Field(min_length=1, max_length=2000)
    native_http_base_url: str | None = Field(default=None, max_length=2000)
    websocket_url: str | None = Field(default=None, max_length=2000)
    api_key: SecretStr | None = None
    scope_id: ConnectionID | None = None

    @field_validator("api_key")
    @classmethod
    def key_length(cls, value):
        if value is not None and (not value.get_secret_value().strip() or len(value.get_secret_value()) > 2000):
            raise ValueError("API Key必须为1至2000字符")
        if value is not None and any(ord(char) < 32 for char in value.get_secret_value()):
            raise ValueError('API Key不能含控制字符')
        return value


class ConnectionUpdate(StrictModel):
    label: str | None = Field(default=None, min_length=1, max_length=80)
    api_key: SecretStr | None = None

    _key_length = field_validator("api_key")(ConnectionCreate.key_length.__func__)


def checked_url(value, provider, protocols=("http", "https"), local_networks=()):
    if not value or any(ord(char) < 33 for char in value):
        raise ValueError("模型地址为空或含空白/控制字符")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        raise ValueError("模型地址格式无效") from None
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in protocols or not host or parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment:
        raise ValueError("模型地址协议无效或含用户信息、查询参数/片段")
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None and (literal.is_unspecified or literal.is_link_local or literal.is_multicast):
        raise ValueError('拒绝未指定、元数据和组播地址')
    if provider == "bailian":
        if host == 'maas.qianwenapi.com':
            raise ValueError('请使用兼容 Base URL 的工作空间域名：HTTP 为 https://工作空间域名/api/v1，WebSocket 为 wss://工作空间域名/api-ws/v1/inference；可点击“按兼容地址填入配音端点”')
        if parsed.scheme not in ("https", "wss") or not (host == "dashscope.aliyuncs.com" or host.endswith(".dashscope.aliyuncs.com") or host.endswith(".maas.aliyuncs.com")):
            raise ValueError("百炼连接只允许认可的HTTPS/WSS阿里云主机")
    else:
        allowed_hosts = {name.strip().lower() for name in os.environ.get("PAPER2VIDEO_MODEL_HOSTS", "").split(",") if name.strip()}
        if host != "localhost" and host not in allowed_hosts:
            try:
                address = ipaddress.ip_address(host)
            except ValueError:
                raise ValueError("兼容模型主机未在服务端白名单中") from None
            networks = [ipaddress.ip_network(network.strip(), strict=False) for network in local_networks]
            if not address.is_loopback and not any(address in network for network in networks):
                raise ValueError("本地模型地址不在服务端允许的私网范围中")
            if address.is_unspecified or address.is_link_local or address.is_multicast:
                raise ValueError("拒绝未指定、元数据和组播地址")
    authority = f"[{host}]" if ":" in host else host
    if port is not None:
        authority += f":{port}"
    return urlunsplit((parsed.scheme, authority, parsed.path.rstrip("/"), "", ""))


class ConnectionStore:
    def __init__(self):
        self._entries = {}

    def _entry(self, connection_id):
        if connection_id == "bailian-default":
            key = os.environ.get("BAILIAN_API_KEY", "").strip()
            base = os.environ.get("BAILIAN_COMPATIBLE_BASE_URL", "").strip()
            if key and base:
                return {"id": connection_id, "label": "百炼环境连接", "provider": "bailian", "source": "env",
                        "scope_id": os.environ.get("BAILIAN_SCOPE_ID", "bailian-default"),
                        "compatible_base_url": checked_url(base, "bailian"),
                        "native_http_base_url": checked_url(os.environ["BAILIAN_NATIVE_HTTP_BASE_URL"], "bailian") if os.environ.get("BAILIAN_NATIVE_HTTP_BASE_URL") else None,
                        "websocket_url": checked_url(os.environ["BAILIAN_WEBSOCKET_URL"], "bailian", ("wss",)) if os.environ.get("BAILIAN_WEBSOCKET_URL") else None,
                        "api_key": key}
        entry = self._entries.get(connection_id)
        if entry is None:
            raise ConfigurationError("connection_id", "连接不存在或服务重启后已失效，请重新绑定", "connection_expired", 409)
        return entry

    @staticmethod
    def public(entry):
        result = {key: value for key, value in entry.items() if key != "api_key"}
        result["key_present"] = bool(entry.get("api_key"))
        result["readiness"] = "configuration_only"
        return result

    def list(self):
        entries = [self.public(entry) for entry in self._entries.values()]
        if os.environ.get("BAILIAN_API_KEY") and os.environ.get("BAILIAN_COMPATIBLE_BASE_URL"):
            entries.insert(0, self.public(self._entry("bailian-default")))
        return entries

    def create(self, payload, local_networks=()):
        values = payload.model_dump(exclude={"api_key"})
        for field in ("compatible_base_url", "native_http_base_url", "websocket_url"):
            if values.get(field):
                try:
                    values[field] = checked_url(values[field], payload.provider, ("wss",) if field == "websocket_url" else ("http", "https"), local_networks)
                except ValueError as exc:
                    raise ConfigurationError(field, str(exc)) from None
        if payload.provider != "bailian" and (payload.native_http_base_url or payload.websocket_url):
            raise ConfigurationError("provider", "兼容连接不使用百炼原生/WS配置")
        if payload.provider == "bailian" and payload.api_key is None:
            raise ConfigurationError("api_key", "百炼连接需要API Key")
        values.update(id="conn_" + uuid.uuid4().hex, source="memory", scope_id=payload.scope_id or "scope_" + uuid.uuid4().hex,
                      api_key=payload.api_key.get_secret_value().strip() if payload.api_key else "")
        self._entries[values["id"]] = values
        return self.public(values)

    def update(self, connection_id, payload):
        entry = dict(self._entry(connection_id))
        if entry["source"] != "memory":
            raise ConfigurationError("connection_id", "环境连接请修改服务端环境变量", status=409)
        if payload.label is not None:
            entry["label"] = payload.label
        if "api_key" in payload.model_fields_set:
            if payload.api_key is None and entry["provider"] == "bailian":
                raise ConfigurationError("api_key", "不能清空百炼Key")
            entry["api_key"] = payload.api_key.get_secret_value().strip() if payload.api_key else ""
        self._entries[connection_id] = entry
        return self.public(entry)

    def delete(self, connection_id):
        if self._entry(connection_id)["source"] != "memory":
            raise ConfigurationError("connection_id", "环境连接不可通过API删除", status=409)
        del self._entries[connection_id]

    def freeze(self, config, roles, previous=None, credentials=None):
        snapshots, keys, entries = {}, {}, {}
        required = {config.roles()[role].connection_id for role in roles if config.roles()[role].connection_id}
        for stage in config.roles().values():
            connection_id = stage.connection_id
            if not connection_id:
                continue
            if connection_id in entries:
                entry = entries[connection_id]
                snapshot = snapshots[connection_id]
            elif connection_id in (previous or {}) and connection_id in (credentials or {}):
                snapshot = copy.deepcopy(previous[connection_id])
                entry = {**snapshot, "api_key": credentials[connection_id]}
            else:
                try:
                    entry = self._entry(connection_id)
                except ConfigurationError:
                    if connection_id in required:
                        raise
                    if connection_id in (previous or {}):
                        snapshots[connection_id] = previous[connection_id]
                    continue
                snapshot = self.public(entry)
                expected = (previous or {}).get(connection_id)
                fields = ("provider", "scope_id", "compatible_base_url", "native_http_base_url", "websocket_url")
                if expected and any(expected.get(field) != snapshot.get(field) for field in fields):
                    if connection_id not in required:
                        snapshots[connection_id] = expected
                        continue
                    raise ConfigurationError("connection_id", "连接与任务快照不一致，请显式重新绑定", "connection_expired", 409)
            if stage.provider != entry["provider"]:
                raise ConfigurationError("connection_id", "连接提供方与模型配置不一致")
            if connection_id in required and stage.mode == "api" and not entry.get("api_key"):
                raise ConfigurationError("connection_id", "API环节缺少连接凭据", "connection_expired", 409)
            if stage.mode == "local" and stage.provider == "openai_compatible":
                parsed = urlsplit(entry["compatible_base_url"])
                try:
                    local = parsed.hostname == "localhost" or ipaddress.ip_address(parsed.hostname).is_loopback or ipaddress.ip_address(parsed.hostname).is_private
                except ValueError:
                    local = False
                if not local:
                    raise ConfigurationError("connection_id", "本地兼容模式必须连接本机或明确允许的私网地址")
            snapshots[connection_id] = snapshot
            entries[connection_id] = entry
            if connection_id in required:
                keys[connection_id] = entry.get("api_key") or ""
        return snapshots, keys
