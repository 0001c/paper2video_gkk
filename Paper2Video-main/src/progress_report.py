"""结构化进度输出（P1）。

为什么要有它：Web 端的进度原先靠 **tail 流水线日志 + 正则匹配页数**，
也就是说 `改一句 print 就失效`。这里让流水线额外写一份机器可读的进度，
Web 端优先读它，从而摆脱对日志文本的依赖（日志仍保留，作为兜底与排查依据）。

三条约束，合起来保证这个改动是「只加外壳、不碰算法」：

1. **默认不生效**：只有设置了环境变量 ``PAPER2VIDEO_PROGRESS_FILE`` 才写。
   从命令行直接跑 ``pipeline.py`` 时行为与改动前**完全一致**（不多产生任何文件）。
2. **绝不抛异常**：写进度失败（磁盘满、权限、编码）绝不能影响正在跑的流水线，
   因此整体有 try/except 兜底 —— 进度是"锦上添花"，不能变成新的故障点。
3. **只追加、不重写**：一行一个 JSON，写完立即落盘。
   体积随页数线性增长（11 页约 30 行、几 KB），不需要轮转。

每行的格式::

    {"ts": 1758140000.0, "stage": 2, "phase": "tts", "done": 7, "total": 11}

``phase`` 取值约定：``stage``（阶段边界）/ ``tts`` / ``cursor`` / ``talking`` / ``merge``。
后端 *_只需要* 认得 ``phase`` 与 ``done``/``total``*，其余字段留给排查。
"""
import json
import os
import time

ENV_VAR = "PAPER2VIDEO_PROGRESS_FILE"

_dir_ready = False


def enabled():
    """是否启用（未设置环境变量时为 False，即完全不影响原行为）。"""
    return bool(os.environ.get(ENV_VAR))


def report(phase, stage=None, done=None, total=None, **extra):
    """追加一行进度。**任何失败都静默忽略。**"""
    target = os.environ.get(ENV_VAR)
    if not target:
        return

    global _dir_ready
    try:
        if not _dir_ready:
            parent = os.path.dirname(os.path.abspath(target))
            if parent:
                os.makedirs(parent, exist_ok=True)
            _dir_ready = True

        record = {"ts": round(time.time(), 3), "phase": phase}
        if stage is not None:
            record["stage"] = stage
        if done is not None:
            record["done"] = done
        if total is not None:
            record["total"] = total
        if extra:
            record.update(extra)

        line = (json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8")
        # O_APPEND + 单次 write：小写入在多进程下也是原子的，不会两行交叉。
        # 当前流水线是串行的（单卡必须按阶段加载/卸载，见 talking_gen 的说明），
        # 但这样写对未来并行更稳。
        fd = os.open(target, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o644)
        try:
            os.write(fd, line)
        finally:
            os.close(fd)
    except Exception:
        # 进度上报绝不能影响流水线本身
        pass


def read_all(path):
    """读取进度文件（Web 端用）。逐行解析，坏行跳过。

    为什么逐行 try：文件是**正在被追加**的，最后一行可能只写了一半
    （虽然单次 write 通常原子，但读取侧不该假设这一点）。
    """
    out = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except Exception:
                    continue
    except Exception:
        return []
    return out
