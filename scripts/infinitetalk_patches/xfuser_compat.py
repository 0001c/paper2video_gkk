"""xfuser 兼容层：单卡（Windows）下提供退化实现，避免被 triton 卡死。

⚠️ 本文件是**补丁源文件**，由 scripts/patch_infinitetalk.py 复制到
InfiniteTalk 仓库的 `wan/xfuser_compat.py`，请勿直接改 InfiniteTalk 里的副本 ——
那份不受版本管理，重建环境就丢了。

为什么需要这一层
----------------
`wan/modules/attention.py` 与 `wan/utils/multitalk_utils.py` 都在**模块顶层**从 xfuser 取
三个符号：

    get_sequence_parallel_rank / get_sequence_parallel_world_size / get_sp_group

而 `import xfuser` 会一路走到 `xfuser/core/sparge_attention/block_mask.py` 的
`import triton`。**triton 官方没有 Windows wheel**（PyPI 上 `pip install triton` 报
"Could not find a version"），于是一个**只服务于可选稀疏注意力**的依赖，把整个模型
拖成不可导入 —— 报错点还落在 xfuser 内部，看不出与 triton 的真实关系。

但关键事实是：**单卡根本用不到 xfuser**。这三个符号在单进程下的取值是确定的：

    序列并行 rank       = 0
    序列并行 world size = 1
    通信 group          = 无

Linux 多卡服务器上 xfuser 能正常安装（triton 有官方 wheel），此时本模块自动走真实实现，
行为与上游完全一致。

安全设计（重要）
----------------
若 xfuser 不可用、而调用方**真的要求多卡**，这里会在导入时**直接抛错**，而不是静默返回
退化值。否则多卡会以「看起来在正常跑、其实每张卡各算一份」的方式错误运行 —— 这种错误
不会报错，只会让结果莫名变差，是最难查的一类问题。
"""

import os

XFUSER_AVAILABLE = False
XFUSER_IMPORT_ERROR = None

try:
    from xfuser.core.distributed import (  # noqa: F401
        get_sequence_parallel_rank,
        get_sequence_parallel_world_size,
        get_sp_group,
    )
    XFUSER_AVAILABLE = True
except Exception as _exc:  # ModuleNotFoundError(triton) 等
    XFUSER_IMPORT_ERROR = _exc

    _WORLD_SIZE = int(os.environ.get("WORLD_SIZE", 1))
    if _WORLD_SIZE > 1:
        raise RuntimeError(
            "检测到 WORLD_SIZE={}（多卡并行），但 xfuser 无法导入：{}\n"
            "多卡下必须使用真实的 xfuser —— 退化实现会让每张卡各算一份，结果错误且不报错。\n"
            "Linux 上安装：pip install xfuser triton".format(_WORLD_SIZE, _exc)
        )

    def get_sequence_parallel_rank():
        """单进程：序列并行 rank 恒为 0。"""
        return 0

    def get_sequence_parallel_world_size():
        """单进程：序列并行度为 1（即不做切分）。"""
        return 1

    def get_sp_group():
        """单进程：没有通信组。"""
        return None
