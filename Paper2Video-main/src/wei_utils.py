import os

from camel.types import ModelPlatformType, ModelType
from camel.configs import ChatGPTConfig, QwenConfig, VLLMConfig, OpenRouterConfig, GeminiConfig

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


def _max_output_tokens():
    """输出 token 上限（可用 ``PAPER2VIDEO_MAX_TOKENS`` 覆盖），默认 32768。

    ⚠️ 为什么从 16384 提到 32768（2026-09-18 实测）——
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
                  "回落到默认 32768：{!r}".format(raw))
    return 32768


def get_agent_config(model_type):
    agent_config = {}
    if model_type in _OPENAI_COMPATIBLE_ALIASES:
        # camel 的 OPENAI_COMPATIBLE_MODEL 分支会创建 OpenAI 客户端，
        # base_url / api_key 从环境变量读取。pv_env 已把 .env 里的
        # OPENAI_API_BASE / OPENAI_API_KEY 同步到多个候选变量名上。
        # max_tokens 必须显式给：DeepSeek-V4.1-Flash 是推理模型，思维链会占用
        # completion_tokens（实测回一个 "OK" 就吃掉 18 token）。camel 的
        # ChatGPTConfig() 默认是空 dict，不传 max_tokens 时由服务端兜底，生成长篇
        # Beamer 代码极易被截断，且截断后的报错点是 latex 编译失败，极难定位。
        agent_config = {
            "model_type": model_type,
            "model_platform": ModelPlatformType.OPENAI_COMPATIBLE_MODEL,
            "model_config": {**ChatGPTConfig().as_dict(),
                             "max_tokens": _max_output_tokens()},
            "max_images": 99,   # DeepSeek-V4.1-Flash 支持 1M 上下文，可整篇论文+全部幻灯片
        }
    elif model_type == 'qwen':
        agent_config = {
            "model_type": ModelType.DEEPINFRA_QWEN_2_5_72B,
            "model_config": QwenConfig().as_dict(),
            "model_platform": ModelPlatformType.DEEPINFRA,
        }
    elif model_type == 'gemini-2.5-pro':
        agent_config = {
            "model_type": ModelType.GEMINI_2_5_PRO,
            "model_config": GeminiConfig().as_dict(),
            "model_platform": ModelPlatformType.GEMINI,
            'max_images': 99
        }
    elif model_type == 'gemini-2.5-flash':
        agent_config = {
            "model_type": ModelType.GEMINI_2_5_FLASH,
            "model_config": GeminiConfig().as_dict(),
            "model_platform": ModelPlatformType.GEMINI,
            'max_images': 99
        }
    elif model_type == 'gemini':
        agent_config = {
            "model_type": ModelType.DEEPINFRA_GEMINI_2_FLASH,
            "model_config": GeminiConfig().as_dict(),
            "model_platform": ModelPlatformType.DEEPINFRA,
            'max_images': 99
        }
    elif model_type == 'phi4':
        agent_config = {
            "model_type": ModelType.DEEPINFRA_PHI_4_MULTIMODAL,
            "model_config": QwenConfig().as_dict(),
            "model_platform": ModelPlatformType.DEEPINFRA,
        }
    elif model_type == 'llama-4-scout-17b-16e-instruct':
        agent_config = {
            'model_type': ModelType.ALIYUN_LLAMA4_SCOUT_17B_16E,
            'model_config': QwenConfig().as_dict(),
            'model_platform': ModelPlatformType.QWEN,
            'max_images': 99
        }
    elif model_type == 'qwen-2.5-vl-72b':
        agent_config = {
            'model_type': ModelType.QWEN_2_5_VL_72B,
            'model_config': QwenConfig().as_dict(),
            'model_platform': ModelPlatformType.QWEN,
            'max_images': 99
        }
    elif model_type == 'gemma':
        agent_config = {
            "model_type": "google/gemma-3-4b-it",
            "model_platform": ModelPlatformType.VLLM,
            "model_config": VLLMConfig().as_dict(),
            "url": 'http://localhost:5555/v1',
            'max_images': 99
        }
    elif model_type == 'llava':
        agent_config = {
            "model_type": "llava-hf/llava-onevision-qwen2-7b-ov-hf",
            "model_platform": ModelPlatformType.VLLM,
            "model_config": VLLMConfig().as_dict(),
            "url": 'http://localhost:8000/v1',
            'max_images': 99
        }
    elif model_type == 'molmo-o':
        agent_config = {
            "model_type": "allenai/Molmo-7B-O-0924",
            "model_platform": ModelPlatformType.VLLM,
            "model_config": VLLMConfig().as_dict(),
            "url": 'http://localhost:8000/v1',
            'max_images': 99
        }
    elif model_type == 'qwen-2-vl-7b':
        agent_config = {
            "model_type": "Qwen/Qwen2-VL-7B-Instruct",
            "model_platform": ModelPlatformType.VLLM,
            "model_config": VLLMConfig().as_dict(),
            "url": 'http://localhost:8000/v1',
            'max_images': 99
        }
    elif model_type == 'vllm_phi4':
        agent_config = {
            "model_type": "microsoft/Phi-4-multimodal-instruct",
            "model_platform": ModelPlatformType.VLLM,
            "model_config": VLLMConfig().as_dict(),
            "url": 'http://localhost:8000/v1',
            'max_images': 99
        }
    elif model_type == 'o3-mini':
        agent_config = {
            "model_type": ModelType.O3_MINI,
            "model_config": ChatGPTConfig().as_dict(),
            "model_platform": ModelPlatformType.OPENAI,
        }
    elif model_type == 'gpt-4.1':
        agent_config = {
            "model_type": ModelType.GPT_4_1,
            "model_config": ChatGPTConfig().as_dict(),
            "model_platform": ModelPlatformType.OPENAI,
        }
    elif model_type == 'gpt-4.1-mini':
        agent_config = {
            "model_type": ModelType.GPT_4_1_MINI,
            "model_config": ChatGPTConfig().as_dict(),
            "model_platform": ModelPlatformType.OPENAI,
        }
    elif model_type == '4o':
        agent_config = {
            "model_type": ModelType.GPT_4O,
            "model_config": ChatGPTConfig().as_dict(),
            "model_platform": ModelPlatformType.OPENAI,
            # "model_name": '4o'
        }
    elif model_type == '4o-mini':
        agent_config = {
            "model_type": ModelType.GPT_4O_MINI,
            "model_config": ChatGPTConfig().as_dict(),
            "model_platform": ModelPlatformType.OPENAI,
        }
    elif model_type == 'o1':
        agent_config = {
            "model_type": ModelType.O1,
            "model_config": ChatGPTConfig().as_dict(),
            "model_platform": ModelPlatformType.OPENAI,
            # "model_name": 'o1'
        }
    elif model_type == 'o3':
        agent_config = {
            "model_type": ModelType.O3,
            "model_config": ChatGPTConfig().as_dict(),
            "model_platform": ModelPlatformType.OPENAI,
        }
    elif model_type == 'gpt-5':
        agent_config = {
            "model_type": ModelType.GPT_5,
            "model_config": ChatGPTConfig().as_dict(),
            "model_platform": ModelPlatformType.OPENAI,
        }
    elif model_type == 'vllm_qwen_vl':
        agent_config = {
            "model_type": "Qwen/Qwen2.5-VL-7B-Instruct",
            "model_platform": ModelPlatformType.VLLM,
            "model_config": VLLMConfig().as_dict(),
            "url": 'http://localhost:7000/v1'
        }
    elif model_type == 'vllm_qwen':
        agent_config = {
            "model_type": "Qwen/Qwen2.5-7B-Instruct",
            "model_platform": ModelPlatformType.VLLM,
            "model_config": VLLMConfig().as_dict(),
            "url": 'http://localhost:8000/v1',
        }
    elif model_type == 'openrouter_qwen_72b':
        agent_config = {
            'model_type': ModelType.OPENROUTER_QWEN_2_5_72B,
            'model_platform': ModelPlatformType.OPENROUTER,
            'model_config': OpenRouterConfig().as_dict(),
        }
    elif model_type == 'openrouter_qwen_vl_72b':
        agent_config = {
            'model_type': ModelType.OPENROUTER_QWEN_2_5_VL_72B,
            'model_platform': ModelPlatformType.OPENROUTER,
            'model_config': OpenRouterConfig().as_dict(),
        }
    elif model_type == 'openrouter_qwen_vl_7b':
        agent_config = {
            'model_type': ModelType.OPENROUTER_QWEN_2_5_VL_7B,
            'model_platform': ModelPlatformType.OPENROUTER,
            'model_config': OpenRouterConfig().as_dict(),
        }
    elif model_type == 'openrouter_qwen_7b':
        agent_config = {
            'model_type': ModelType.OPENROUTER_QWEN_2_5_7B,
            'model_platform': ModelPlatformType.OPENROUTER,
            'model_config': OpenRouterConfig().as_dict(),
        }
    else:
        agent_config = {
            'model_type': model_type,
            'model_platform': ModelPlatformType.OPENAI_COMPATIBLE_MODEL,
            'model_config': None
        }
    
    return agent_config