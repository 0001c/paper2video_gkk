"""环境、兼容 API 别名及本地模型路径。

读取仓库 .env，不覆盖已有环境配置；缓存和媒体暂存使用纯英文路径。
"""

import os
import sys
from pathlib import Path

_SRC_DIR = Path(__file__).resolve().parent
PACKAGE_ROOT = _SRC_DIR.parent          # Paper2Video-main/
REPO_ROOT = PACKAGE_ROOT.parent         # 仓库根，.env 在这里


# ── 让 stdout / stderr 永不因编码问题崩溃 ──────────────────────────────────
# 上游代码里有几处带 emoji 的 print（实测：cursor_render.py 的 "\n\u2705 Done!"、
# cursor_gen.py 的两处 "\u26a0\ufe0f"、slide_code_gen 的一处）。
# 当 stdout 被重定向到文件时，Python 按**平台默认编码**输出（Windows 上是 GBK），
# emoji 编不出来就抛 UnicodeEncodeError —— 而它在流水线**最末端**才触发
# （合片完成后那一行 print），实测把 20 分钟、11 页数字人全做完的活儿直接打断。
#
# 两层保护，缺一不可：
#   ① 启动器侧设 `PYTHONIOENCODING=utf-8`（web/app.py 的子进程环境已设）；
#   ② 这里把 errors 设成 `replace` 兜底 —— 命令行直接跑、或别的方式启动时
#      没设环境变量，也只是把那个字符打成 "?"，而不是崩掉整条流水线。
#
# 放在 pv_env 里是因为它被 9 个模块导入（pipeline / pipeline_light / cursor_gen /
# cursor_render / speech_gen / talking_gen / subtitle_render / wei_utils …），
# 于是所有入口自动获得这层保护。放在模块级，import 即生效。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")
    except Exception:
        pass        # stdout 可能是 None 或被替换成非 TextIOWrapper，忽略即可


def _parse_dotenv(path):
    """极简 .env 解析：支持 KEY=VALUE、# 注释、空行、值两侧引号。"""
    values = {}
    if not path.is_file():
        return values
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key:
            values[key] = value
    return values


def load_env(dotenv_path=None):
    """把 .env 写入 os.environ（已存在的环境变量优先，不被覆盖）。

    重复调用是安全的；返回实际注入的键值对，便于调试。
    """
    path = Path(dotenv_path) if dotenv_path else (REPO_ROOT / ".env")
    applied = {}
    for key, value in _parse_dotenv(path).items():
        if os.environ.get(key):
            continue
        if value == "":
            # 空值也写进去，让下游能区分「未配置」与「缺变量」
            os.environ.setdefault(key, "")
            continue
        os.environ[key] = value
        applied[key] = value
    return applied


# 模块导入即生效：保证任何 import pv_env 的脚本都拿到正确路径
load_env()


def _sync_api_aliases():
    """把 .env 里的 OPENAI_* 同步到各类候选变量名。

    camel 的不同版本在构造 OpenAI 兼容客户端时读的环境变量名不一致
    （``OPENAI_API_BASE`` / ``OPENAI_BASE_URL`` / ``OPENAI_COMPATIBLE_MODEL_API_BASE``），
    这里一次性全部写齐，避免为了一个变量名去翻 camel 源码。
    """
    pairs = (
        ("OPENAI_API_KEY",
         ("OPENAI_COMPATIBLE_MODEL_API_KEY", "DEEPSEEK_API_KEY")),
        ("OPENAI_API_BASE",
         ("OPENAI_BASE_URL", "OPENAI_COMPATIBLE_MODEL_API_BASE", "DEEPSEEK_API_BASE")),
    )
    for primary, aliases in pairs:
        value = os.environ.get(primary, "").strip()
        if not value:
            continue
        for alias in aliases:
            os.environ.setdefault(alias, value)


_sync_api_aliases()

# ── 缓存目录：一律指向 E 盘 SSD ──
# 读权重是随机 IO，机械盘（F 盘）会让加载时间放大十倍以上，
# 因此这里全部默认走 E 盘；F 盘只用于放数据集归档。
_DEFAULT_ROOT = r"E:\ai-models"
HF_HOME = os.environ.get("HF_HOME") or os.path.join(_DEFAULT_ROOT, "hf")
MODELSCOPE_CACHE = os.environ.get("MODELSCOPE_CACHE") or os.path.join(_DEFAULT_ROOT, "modelscope")
TORCH_HOME = os.environ.get("TORCH_HOME") or os.path.join(_DEFAULT_ROOT, "torch")
WEIGHTS_DIR = os.environ.get("PAPER2VIDEO_WEIGHTS") or os.path.join(_DEFAULT_ROOT, "weights")

# 中间产物用纯英文临时目录。
# 项目路径含中文，而 ffmpeg 的 subtitles/ass 滤镜在 Windows 上无法处理
# 非 ASCII 路径 —— 字幕文件必须先落在纯英文目录里，再以相对文件名引用。
TMP_DIR = os.environ.get("PAPER2VIDEO_TMP") or os.path.join(_DEFAULT_ROOT, "tmp")

for _var, _val in (("HF_HOME", HF_HOME), ("MODELSCOPE_CACHE", MODELSCOPE_CACHE),
                   ("TORCH_HOME", TORCH_HOME), ("PAPER2VIDEO_WEIGHTS", WEIGHTS_DIR)):
    os.environ.setdefault(_var, _val)

# ── 外部工具目录前置到 PATH ──
# scripts/setup_tools.py 把 tectonic.exe 装在这里。前置 PATH 后，
# slide_code_gen_select_improvement.compile_tex() 里的
# subprocess.run(["tectonic", ...]) 无需改代码即可找到它；
# 也比改系统环境变量更干净、可回滚。
TOOLS_BIN = os.path.join(_DEFAULT_ROOT, "bin")
if os.path.isdir(TOOLS_BIN):
    _path_parts = os.environ.get("PATH", "").split(os.pathsep)
    if TOOLS_BIN not in _path_parts:
        os.environ["PATH"] = TOOLS_BIN + os.pathsep + os.environ.get("PATH", "")

# ── 中文路径兼容层 ──
# 项目路径含中文（E:\Development\26年\...）。Python 自己的 IO 走宽字符 API 没问题，
# 但**编译型扩展在 C/C++ 层用窄字符路径打开文件会失败**。实测 nagisa 的模型由
# DyNet(C++) 加载，报 "Could not read model from ...\26年\...\nagisa_v001.model"；
# 而 qwen_asr 在模块顶层 import nagisa（只为日文对齐），一个可选功能就把整个
# qwen_asr 拖成不可导入。
#
# scripts/fix_cjk_path.py 会把这类包镜像到纯英文目录。这里把该目录前置到
# sys.path，使 import 命中英文副本。必须在任何 import qwen_asr 之前生效，
# 所以放在模块导入期执行（pv_env 是流水线的首个导入）。
#
# 用 insert 而非 append：site-packages 已在 sys.path 中，只有前置才能覆盖它。
VENDOR_DIR = os.environ.get("PAPER2VIDEO_VENDOR") or os.path.join(_DEFAULT_ROOT, "vendor")
if os.path.isdir(VENDOR_DIR) and VENDOR_DIR not in sys.path:
    sys.path.insert(0, VENDOR_DIR)


def model_path(repo_id):
    """返回本地权重目录（若已下载），否则原样返回 repo_id 交给缓存解析。

    ``scripts/download_models.py`` 把 ``org/name`` 落成 ``weights/org__name``，
    这里按同一规则反查。
    """
    local = Path(WEIGHTS_DIR) / repo_id.replace("/", "__")
    if local.is_dir() and any(local.iterdir()):
        return str(local)
    return repo_id


def require_api_key(name="OPENAI_API_KEY"):
    """取 API Key，缺失时给出可操作的报错，而不是让请求在第 200 行才失败。"""
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(
            "环境变量 {} 未配置。请在 {} 里填入，或先执行 "
            "./.venv/Scripts/python.exe scripts/download_models.py --list 查看模型清单。"
            .format(name, REPO_ROOT / ".env")
        )
    return value


def gpu_list(default=None):
    """解析 GPU 列表。

    接受三种形态，因为两个流水线传进来的类型并不一致，而调用方都是**直接透传**：

    * ``None``      —— 读环境变量 ``PAPER2VIDEO_GPU_LIST``
    * ``"0,1"``     —— ``pipeline_light.py`` 的 ``--gpu_list`` 是 str
    * ``[0, 1]``    —— ``pipeline.py`` 的 ``--gpu_list`` 走 str2list，是 list

    原先只做 ``str(raw).split(",")``，于是 ``pipeline.py`` 传 list 时
    ``str([0])`` 得到 ``"[0]"`` → ``int("[0]")`` 抛 ValueError，**stage 2
    的光标定位必崩**（pipeline_light 传 str 所以没暴露）。

    另外，``--gpu_list`` 默认空串会让 ``str2list("")`` 抛 ValueError，
    或让 ``num_gpus = len(gpu_list)`` 得到 0 而触发除零。这里统一兜底为 ``[0]``。
    """
    raw = default if default is not None else (os.environ.get("PAPER2VIDEO_GPU_LIST") or "")
    if isinstance(raw, (list, tuple, set)):
        parts = [str(item).strip() for item in raw if str(item).strip() != ""]
    else:
        parts = [piece.strip() for piece in str(raw).split(",") if piece.strip() != ""]
    if not parts:
        return [0]
    return [int(piece) for piece in parts]


def print_banner():
    print("=" * 72)
    print("Paper2Video 环境")
    print("  仓库根     : {}".format(REPO_ROOT))
    print("  权重目录   : {}".format(WEIGHTS_DIR))
    print("  HF_HOME    : {}  (endpoint={})".format(HF_HOME, os.environ.get("HF_ENDPOINT", "-")))
    print("  GPU 列表   : {}".format(gpu_list()))
    try:
        import torch
        if torch.cuda.is_available():
            cap = torch.cuda.get_device_capability()
            print("  torch      : {} (cuda {}) 设备 {} sm_{}{}".format(
                torch.__version__, torch.version.cuda, torch.cuda.get_device_name(0), cap[0], cap[1]))
        else:
            print("  torch      : {} (CUDA 不可用)".format(torch.__version__))
    except Exception as exc:
        print("  torch      : 未安装或不可用 ({})".format(type(exc).__name__))
    print("=" * 72)


if __name__ == "__main__":
    print_banner()
