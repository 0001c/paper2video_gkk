"""共享的兼容 API 模型配置；保留输出上限与上下文窗口覆盖。"""


import os


from camel.types import ModelPlatformType


from camel.configs import ChatGPTConfig


import pv_env


_OPENAI_COMPATIBLE_ALIASES = (
    'deepseek-flash',            # 本项目默认：DeepSeek-V4.1-Flash（2026-09 GA，MIT 权重）
    'deepseek-v4-flash',         # 已退役的旧 id，仍路由到 V4.1-Flash
    'deepseek-chat',
    'deepseek-reasoner',
)


_CONTEXT_WINDOWS = {
    'deepseek-flash': 1_000_000,
    'deepseek-v4-flash': 1_000_000,
    'deepseek-chat': 128_000,
    'deepseek-reasoner': 128_000,
}


def _register_context_windows():
    """登记已知模型的上下文窗口，支持环境变量覆盖。"""
    override = os.environ.get('PAPER2VIDEO_CONTEXT_WINDOW')
    if override:
        try:
            value = int(override)
        except ValueError:
            print("[wei_utils] PAPER2VIDEO_CONTEXT_WINDOW={!r} 不是整数，已忽略".format(override))
        else:
            for name in _CONTEXT_WINDOWS:
                _CONTEXT_WINDOWS[name] = value

    try:
        from camel.types.unified_model_type import UnifiedModelType
    except Exception:
        return                      # camel 结构变了也不能拖垮流水线

    original = UnifiedModelType.__dict__.get('token_limit')
    if not isinstance(original, property) or original.fget is None:
        return
    if getattr(original.fget, '_p2v_patched', False):
        return                      # 已被打过（重复 import 时幂等）

    def token_limit(self):
        known = _CONTEXT_WINDOWS.get(str(self))
        if known is not None:
            return known
        return original.fget(self)

    token_limit._p2v_patched = True
    UnifiedModelType.token_limit = property(token_limit)


_register_context_windows()


def _max_output_tokens():
    """返回已知兼容模型的输出上限，默认 65536。"""
    raw = os.environ.get("PAPER2VIDEO_MAX_TOKENS", "").strip()
    if raw:
        try:
            value = int(raw)
            if value > 0:
                return value
        except ValueError:
            print("[wei_utils] PAPER2VIDEO_MAX_TOKENS 不是正整数，"
                  "回落到默认 65536：{!r}".format(raw))
    return 65536


def get_agent_config(model_type):
    """为所选模型构造兼容 API 配置，端点由环境变量提供。"""
    model_config = ChatGPTConfig().as_dict()
    if model_type in _OPENAI_COMPATIBLE_ALIASES:
        model_config["max_tokens"] = _max_output_tokens()
    return {
        "model_type": model_type,
        "model_platform": ModelPlatformType.OPENAI_COMPATIBLE_MODEL,
        "model_config": model_config,
        "max_images": 99,
        "url": os.environ.get('OPENAI_COMPATIBLE_MODEL_API_BASE') or os.environ.get('OPENAI_BASE_URL'),
    }
