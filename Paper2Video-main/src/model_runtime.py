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
    if not isinstance(snapshot, dict) or set(snapshot) - {"model_config", "model_config_origin", "model_connections", "derived_policy"}:
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
        for name in ("OPENAI_API_KEY", "OPENAI_COMPATIBLE_MODEL_API_KEY", "OPENAI_API_BASE", "OPENAI_BASE_URL", "OPENAI_COMPATIBLE_MODEL_API_BASE", "DEEPSEEK_API_KEY", "DEEPSEEK_API_BASE"):
            os.environ.pop(name, None)
        primary = "text" if "text" in roles else "vision" if "vision" in roles else None
        if primary:
            stage = config.roles()[primary]
            key = os.environ[f"PAPER2VIDEO_ROLE_{primary.upper()}_API_KEY"]
            base = connections[stage.connection_id]["compatible_base_url"]
            for name in ("OPENAI_API_KEY", "OPENAI_COMPATIBLE_MODEL_API_KEY"):
                os.environ[name] = key
            for name in ("OPENAI_API_BASE", "OPENAI_BASE_URL", "OPENAI_COMPATIBLE_MODEL_API_BASE"):
                os.environ[name] = base
        os.environ['PAPER2VIDEO_MODEL_CONFIG_ORIGIN'] = 'v2'
    args.model_name_t = config.stages.text.model
    args.model_name_v = config.stages.vision.model
    args.tts_model = config.stages.tts.model
    if hasattr(args, "generate_talking"):
        args.generate_talking = config.avatar.enabled
    elif config.avatar.enabled:
        raise ValueError("light流水线不能启用数字人")
    return snapshot
