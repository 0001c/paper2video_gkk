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


def get_agent_config(model_type, role=None, snapshot=None):
    """v2按角色读取无秘密快照；旧任务保留环境配置。"""
    from model_runtime import role_settings
    settings = role_settings(snapshot, role) if role else None
    if not role and os.environ.get('PAPER2VIDEO_MODEL_CONFIG_ORIGIN') == 'v2':
        raise ValueError('v2模型调用必须指定角色和任务快照')
    if settings:
        return {**settings, 'model_platform': ModelPlatformType.OPENAI_COMPATIBLE_MODEL, 'max_images': 99}
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


def create_agent(model_config):
    key_env = model_config.get('api_key_env')
    if key_env:
        api_key = os.environ.get(key_env)
        if not api_key:
            raise ValueError('任务角色凭据缺失，禁止回落全局Key')
    else:
        if os.environ.get('PAPER2VIDEO_MODEL_CONFIG_ORIGIN') == 'v2':
            raise ValueError('v2模型调用缺少角色凭据引用')
        api_key = os.environ.get('OPENAI_COMPATIBLE_MODEL_API_KEY') or os.environ.get('OPENAI_API_KEY')
    from camel.agents import ChatAgent
    from camel.models import ModelFactory
    model = ModelFactory.create(model_platform=model_config['model_platform'], model_type=model_config['model_type'],
                                model_config_dict=model_config.get('model_config'), url=model_config.get('url'), api_key=api_key,
                                **({'max_retries': 0} if model_config.get('provider') == 'bailian' else {}))
    agent = ChatAgent(model=model, system_message='', token_limit=model_config.get('context_window'))
    if model_config.get('provider') == 'bailian':
        from web import model_costs
        original_step = agent.step
        def measured_step(*args, **kwargs):
            ticket = model_costs.begin(model_config['role'], model_config['model_type'], model_config['url'])
            response = original_step(*args, **kwargs)
            model_costs.finish(ticket, (getattr(response, 'info', None) or {}).get('usage'))
            return response
        agent.step = measured_step
    return agent
