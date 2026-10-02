import os

from camel.types import ModelPlatformType
from camel.configs import ChatGPTConfig

# 必须先导入 pv_env：它负责加载 .env、把 OPENAI_API_KEY / OPENAI_API_BASE
# 同步到 camel 读到的那些环境变量名上。若本模块被单独导入（不经 pipeline），
# 少了这一行就会拿到空 Key，报错点还会推迟到真正发请求时。
import pv_env  # noqa: F401

# 走 OpenAI 兼容协议的自建/第三方服务：模型名即 API 的 model id，平台统一为
# OPENAI_COMPATIBLE_MODEL，鉴权与端点由环境变量提供（见 pv_env 的环境变量别名）。
_OPENAI_COMPATIBLE_ALIASES = (
    'deepseek-flash',            # 本项目默认：DeepSeek-V4.1-Flash（2026-09 GA，MIT 权重）
    'deepseek-v4-flash',         # 已退役的旧 id，仍路由到 V4.1-Flash
    'deepseek-chat',
    'deepseek-reasoner',
)

# 上下文窗口（token）。只为消掉一行**看着像错误的 warning**，不参与任何业务逻辑。
#
# camel 的 UnifiedModelType 对"不在它 ModelType 表里的名字"会打一行：
#   Unknown model 'deepseek-flash': context window size not defined.
#   Defaulting to 999_999_999.
# 这行不是错误：它只说明 camel 没登记这个名字，于是把上下文窗口当成 1e9。
# 已登记的名字会返回真实值（实测 gpt-4o -> 128000）。
#
# 影响面很小 —— token_limit 只被 camel 的上下文摘要逻辑消费
# （num_tokens > token_limit 时才触发），而本项目每个阶段都是**一次性 prompt**，
# 远达不到上限、永远不会触发。但两点仍值得修：
#   ① 日志里少一行"像错误"的 warning，演示与排查时不会被误导；
#   ② 万一将来做多轮对话，camel 手里是一个真实上限，而不是 1e9（1e9 意味着
#      **永远不触发摘要**，等于这层保护形同虚设）。
_CONTEXT_WINDOWS = {
    'deepseek-flash': 1_000_000,
    'deepseek-v4-flash': 1_000_000,
    'deepseek-chat': 128_000,
    'deepseek-reasoner': 128_000,
}


def _register_context_windows():
    """把上面的表补进 camel；失败不影响主流程（纯日志/元数据层面的改进）。

    实现方式刻意保守：**只覆盖基类属性，且先查我们的表、查不到再交回原实现**。
    已登记的模型是 ``ModelType`` 枚举成员、自带自己的 ``token_limit`` property，
    因此完全不受影响（这一点实测确认过：gpt-4o 仍返回 128000）。
    另外 fallback 保留了 camel 原来的 warning，所以别的未知模型行为不变。

    可用 ``PAPER2VIDEO_CONTEXT_WINDOW`` 统一覆盖（换模型/换服务商时不用改代码）。
    """
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
    """输出 token 上限（可用 ``PAPER2VIDEO_MAX_TOKENS`` 覆盖），默认 65536。

    ⚠️ 为什么从 16384 提到 32768、再到 65536（2026-09-18 两次实测）——

    第二次（提到 65536 的起因）：用一篇 **24 页 / 6.6 万字符**的 arXiv 论文
    （波斯语 ASR 语料库）跑结构化，`completion_tokens=32768` **正好贴死上限**，
    `finish_reason=length` —— 产物 main.tex **没有 \\end{document}**、
    尾部断在一张表中间，tectonic 报
    `File ended while scanning use of \\environment tblr`。
    思维链那次吃掉约 3.5 万字符。**接口实测接受 max_tokens 到 131072**，
    所以上限可以给得更宽。注意：上限只是"允许的最小值上界"，不产出就不计费。

    第一次（提到 32768 的起因）——
    跑 `gkk_en_zh`（11 张幻灯片 + **中文**讲稿）时报的是
    「VLM 连续 3 次未能给出可用的讲稿」，**看起来完全像网关抖动**。
    但末次 usage 是 `completion_tokens=16386`，**正好贴死在旧上限 16384 上**：
    输出被截断成不完整内容 → camel 解析不出消息（`msgs=[]`）→
    被判空逻辑归类成「返回空响应」。**根因是上限太小，不是网络问题。**

    为什么英文能过、中文撞顶：DeepSeek-V4.1-Flash 是**推理模型**，
    思维链计入 completion_tokens（实测回一个 "OK" 就吃掉 18 token）。
    英文讲稿约 11k token 能过；中文要求 80~110 汉字/页 × 11 页，输出量更大。

    排查这类「空响应」时**先看 usage 里的 completion_tokens 是否贴着上限**，
    再怀疑网络 —— 两者症状一模一样，判错方向会白折腾一整轮。
    """
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
