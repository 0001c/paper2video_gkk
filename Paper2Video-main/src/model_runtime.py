"""读取无秘密任务快照，拒绝未实现的模型路线。"""

import json
import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from web.model_config import ConfigurationError, PipelineModelConfig, ensure_implemented, reject_embedded_secrets, required_roles


def load_model_snapshot(filename):
    path = Path(filename)
    if path.stat().st_size > 262144:
        raise ValueError("模型快照超过256KiB")
    snapshot = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(snapshot, dict) or set(snapshot) - {"model_config", "model_config_origin", "model_connections", "derived_policy", "voice_resource"}:
        raise ValueError("模型快照字段无效")
    reject_embedded_secrets(snapshot, "model_snapshot")
    config = PipelineModelConfig.model_validate(snapshot["model_config"])
    return snapshot, config


def apply_model_snapshot(args):
    if not args.model_config:
        return None
    snapshot, config = load_model_snapshot(args.model_config)
    origin = snapshot.get("model_config_origin")
    if origin not in ("legacy", "v2"):
        raise ValueError("模型快照来源无效")
    pipeline_config = {"stages": json.loads(args.stage), "review_before_render": getattr(args, "review_before_render", False),
                       "reuse_script": getattr(args, "reuse_script", False), "require_reuse_script": getattr(args, "require_reuse_script", False),
                       "ref_audio": args.ref_audio, "ref_text": args.ref_text}
    roles = required_roles(config, pipeline_config)
    ensure_implemented(config, [role for role in roles if not (origin == "legacy" and role == "avatar")])
    if origin == "legacy":
        if config.avatar.mode != "local" or config.avatar.provider != "musetalk" or config.avatar.model != "musetalk":
            raise ValueError("旧路线快照不能声明云端数字人")
    else:
        connections = snapshot.get("model_connections") or {}
        for role in roles:
            stage = config.roles()[role]
            if stage.connection_id and (stage.connection_id not in connections or not os.environ.get(f"PAPER2VIDEO_ROLE_{role.upper()}_API_KEY")):
                raise ConfigurationError(f"model_config.{role}", "任务级连接凭据缺失，禁止回落全局Key", "connection_expired", 409)
        for name in list(os.environ):
            if name.startswith(('OPENAI_', 'DEEPSEEK_')):
                os.environ.pop(name)
        os.environ['PAPER2VIDEO_MODEL_CONFIG_ORIGIN'] = 'v2'
    args.required_model_roles = roles
    args.model_name_t = config.stages.text.model
    args.model_name_v = config.stages.vision.model
    args.tts_model = config.stages.tts.model
    if hasattr(args, "generate_talking"):
        args.generate_talking = config.avatar.enabled
        if hasattr(args, "model_name_talking"):
            args.model_name_talking = config.avatar.model
    elif config.avatar.enabled:
        raise ValueError("light流水线不能启用数字人")
    return snapshot


def role_settings(snapshot, role):
    if not snapshot or snapshot.get('model_config_origin') != 'v2':
        if os.environ.get('PAPER2VIDEO_MODEL_CONFIG_ORIGIN') == 'v2':
            raise ConfigurationError(f'model_config.{role}', 'v2调用缺少任务快照，禁止借用全局配置')
        return None
    config = PipelineModelConfig.model_validate(snapshot['model_config'])
    stage = config.roles()[role]
    connection = (snapshot.get('model_connections') or {}).get(stage.connection_id)
    field = f'config.model_config.stages.{role}'
    if not connection or not connection.get('compatible_base_url') or connection.get('provider') != stage.provider:
        raise ConfigurationError(field + '.connection_id', '任务连接快照缺失或提供方不一致', 'connection_expired', 409)
    key_env = f'PAPER2VIDEO_ROLE_{role.upper()}_API_KEY'
    if not os.environ.get(key_env):
        raise ConfigurationError(field + '.connection_id', '任务角色凭据缺失，禁止回落全局Key', 'connection_expired', 409)
    parameters = stage.parameters.model_dump(exclude_none=True)
    if 'max_output_tokens' in parameters:
        parameters['max_tokens'] = parameters.pop('max_output_tokens')
    limits = (connection.get('model_limits') or {}).get(stage.model, {})
    if parameters.get('max_tokens', 0) > limits.get('max_output_tokens', 131072):
        raise ConfigurationError(field + '.parameters.max_output_tokens', '超过该型号目录记录的输出上限')
    if stage.provider == 'bailian' and stage.model in ('qwen3.8-flash', 'qwen3.8-max', 'qwen3-vl-plus'):
        parameters['extra_body'] = {'enable_thinking': False}
    return {'model_type': stage.model, 'url': connection['compatible_base_url'], 'api_key_env': key_env,
            'role': role, 'provider': stage.provider,
            'model_config': parameters, 'connection_id': stage.connection_id, 'scope_id': connection.get('scope_id'),
            'context_window': limits.get('context_window')}
