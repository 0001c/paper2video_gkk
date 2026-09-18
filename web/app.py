"""Paper2Video Web 服务（P0）。

设计上必须守住的四条（都写在方案文档里，这里集中复述一遍，避免改代码时走偏）：

1. **只监听 127.0.0.1**。这个界面上有 API key 和文件系统访问，
   一旦监听 0.0.0.0，同网络下任何人都能读到你 jobs/ 里的全部产物。
2. **单 worker 串行队列**。单卡 16GB，两个任务同时跑必然 OOM。
   多组合是"排队"而不是"并行"。
3. **流水线走子进程，且不经 shell**。显存释放靠进程退出保证；
   `create_subprocess_exec(argv...)` 而不是 `shell=True`，
   因为本机 MSYS 的 `cd` 遇到含中文的路径会失败，且 shell 会引入转义问题。
4. **不需要的开关绝不暴露**。例如 `--if_tree_search`：
   `pipeline.py:31` 那行 import 是注释掉的，而默认值 True 会走 `latex_code_gen`
   → 直接 NameError。所以它在这里被**写死为 False**，不出现在任何表单里。

启动：
    python web/app.py            # 默认 127.0.0.1:8848
"""
import asyncio
import io
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time
import uuid

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JOBS_ROOT = os.path.join(REPO_ROOT, "jobs")
P2V_SRC = os.path.join(REPO_ROOT, "Paper2Video-main", "src")
SCRIPTS = os.path.join(REPO_ROOT, "scripts")
VENV_PY = os.path.join(REPO_ROOT, ".venv", "Scripts", "python.exe")
if not os.path.isfile(VENV_PY):                       # Linux 上路径不同
    VENV_PY = os.path.join(REPO_ROOT, ".venv", "bin", "python")

sys.path.insert(0, SCRIPTS)
sys.path.insert(0, P2V_SRC)

# 结构化进度的读写约定与流水线共用同一个模块（progress_report），
# 避免两边对字段名/取值的理解跑偏。放 import 在这里是因为它依赖上面的 sys.path。
import progress_report                                    # noqa: E402

os.makedirs(JOBS_ROOT, exist_ok=True)
app = FastAPI(title="Paper2Video Web", version="0.1.0")

# ──────────────────────────────────────────────────────────────
# 会话级 API key（内存，不落盘）
# ──────────────────────────────────────────────────────────────
# 为什么不落盘：这个是本地/内网服务，key 写进文件就多一份泄漏面。
# 需要持久化的场景由使用者自己在仓库根的 .env 里配（pv_env 会读）。
_SESSION_KEYS = {"api_key": "", "api_base": ""}


def _mask(value):
    """只回显前后各 4 位 —— 永远不把完整 key 发给前端。"""
    if not value:
        return ""
    if len(value) <= 10:
        return "*" * len(value)
    return "{}...{}".format(value[:4], value[-4:])


# ──────────────────────────────────────────────────────────────
# 任务注册表
# ──────────────────────────────────────────────────────────────
def _job_dir(job_id):
    return os.path.join(JOBS_ROOT, job_id)


def _load_job(job_id):
    path = os.path.join(_job_dir(job_id), "job.json")
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="任务不存在：{}".format(job_id))
    with io.open(path, encoding="utf-8") as handle:
        return json.load(handle)


def _save_job(job):
    job["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    with io.open(os.path.join(_job_dir(job["id"]), "job.json"), "w",
                 encoding="utf-8", newline="") as handle:
        json.dump(job, handle, ensure_ascii=False, indent=2)


# 任务记录有两个并发写入者：队列 worker（任务结束时按退出码写
# done/failed + exit_code + elapsed_min）与 HTTP 取消接口。
# 两边若是各自"先 load、再改、再 save"，中间只要有一个 await
# （取消接口里就有 proc.wait()），后写的那份就会用**旧快照**
# 把对方刚写的内容整个覆盖 —— 旧快照里根本没有对方新增的字段。
# 实测后果有两种，取决于时序：
#   · 取消接口后写 → exit_code / elapsed_min 凭空消失
#   · worker 后写   → "已取消"被改写成"失败"
# 所以统一走这个"读-改-写"的原子入口，且**只更新明确要改的字段**。
_JOB_LOCK = asyncio.Lock()


async def _update_job(job_id, **fields):
    """原子地读-改-写任务记录。返回更新后的记录。"""
    async with _JOB_LOCK:
        job = _load_job(job_id)
        job.update(fields)
        _save_job(job)
        return job


# 图片格式按**文件头**判断，不信扩展名（.png 里装 JPEG 的情况很常见）
_IMAGE_MAGIC = (
    (b"\x89PNG\r\n\x1a\n", "png"),
    (b"\xff\xd8\xff", "jpeg"),
    (b"BM", "bmp"),
)


def _sniff_image(file_path):
    """返回 png / jpeg / webp / bmp；不是图片则返回 None。"""
    try:
        with io.open(file_path, "rb") as handle:
            head = handle.read(16)
    except Exception:
        return None
    for magic, name in _IMAGE_MAGIC:
        if head.startswith(magic):
            return name
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    return None


def _list_jobs():
    jobs = []
    for name in os.listdir(JOBS_ROOT):
        path = os.path.join(JOBS_ROOT, name, "job.json")
        if os.path.isfile(path):
            try:
                with io.open(path, encoding="utf-8") as handle:
                    jobs.append(json.load(handle))
            except Exception:
                continue
    jobs.sort(key=lambda j: j.get("created_at", ""), reverse=True)
    return jobs


# ──────────────────────────────────────────────────────────────
# 进度解析：从流水线日志里读进度
# ──────────────────────────────────────────────────────────────
# 为什么 P0 用日志解析而不是给流水线加结构化输出：
# 零改造就能拿到页级进度（日志格式是稳定的），先把闭环跑通；
# 结构化 progress.jsonl 放 P1，那时才需要动流水线代码。
_STAGE_PATTERNS = [
    (re.compile(r"第 (\d+) 页合成完成"), "tts"),
    (re.compile(r"第 (\d+) 页完成"), "talking"),
]


def _total_pages(job_dir):
    """总页数：优先从渲染出的 PDF 取，其次数幻灯片图。"""
    pdf = os.path.join(job_dir, "result", "slides_rendered.pdf")
    if os.path.isfile(pdf):
        try:
            import pymupdf
            doc = pymupdf.open(pdf)
            count = doc.page_count
            doc.close()
            return count
        except Exception:
            pass
    img_dir = os.path.join(job_dir, "result", "slide_imgs")
    if os.path.isdir(img_dir):
        return len([f for f in os.listdir(img_dir) if f.endswith(".png")])
    return 0


# ---- P1：结构化进度 → 界面文案 ----
# progress.jsonl 里的 phase 取值由流水线侧约定，见 src/progress_report.py
_PHASE_LABEL = {
    "slides": "生成幻灯片",
    "script": "生成讲稿",
    "tts": "合成语音",
    "cursor": "光标定位",
    "talking": "生成数字人",
    "merge": "合成视频",
}


def _read_tail(job_dir, count=6):
    """日志尾巴。

    无论用哪个进度来源都会读它 —— 它承载的是排查信息（报错、警告、
    护栏触发记录），不只是进度数字。
    """
    log_path = os.path.join(job_dir, "run.log")
    if not os.path.isfile(log_path):
        return []
    try:
        text = io.open(log_path, encoding="utf-8", errors="replace").read()
    except Exception:
        return []
    return [ln.strip() for ln in text.splitlines() if ln.strip()][-count:]


def _progress_from_jsonl(job, job_dir):
    """从流水线主动写的 progress.jsonl 读进度。文件不存在时返回 None。

    P1 起这是**首选来源**：它是机器可读的，不像日志文本那样"改一句 print 就失效"。
    同时它自带 total（总页数），不再依赖去数 slides_rendered.pdf 的页数。
    """
    path = os.path.join(job_dir, "progress.jsonl")
    if not os.path.isfile(path):
        return None
    records = progress_report.read_all(path)
    if not records:
        return None

    info = {"source": "jsonl", "total_pages": 0, "tts_done": 0, "talking_done": 0,
            "phase": "", "last_log_at": None, "stalled_seconds": None}
    for rec in records:
        total = rec.get("total")
        if isinstance(total, int):
            info["total_pages"] = max(info["total_pages"], total)
        done = rec.get("done")
        if not isinstance(done, int):
            continue
        if rec.get("phase") == "tts":
            info["tts_done"] = max(info["tts_done"], done)
        elif rec.get("phase") == "talking":
            info["talking_done"] = max(info["talking_done"], done)

    info["phase"] = _PHASE_LABEL.get(records[-1].get("phase"), "")
    ts = records[-1].get("ts")
    if isinstance(ts, (int, float)):
        # 用最后一条**记录**的时间戳判断"还在不在动"，比日志 mtime 更贴切：
        # 日志里混着各子进程的输出，而进度是流水线自己写的。
        info["stalled_seconds"] = int(max(0, time.time() - ts))
        info["last_log_at"] = time.strftime("%H:%M:%S", time.localtime(ts))
    return info


def _progress_from_log(job, job_dir):
    """兜底：tail 日志 + 正则匹配页数。

    这条路径零改造就能用，但**改一句 print 就会失效**（P1 之前是唯一来源，
    现在降级为兜底：老任务目录里没有 progress.jsonl 时靠它）。
    """
    log_path = os.path.join(job_dir, "run.log")
    if not os.path.isfile(log_path):
        return None
    try:
        text = io.open(log_path, encoding="utf-8", errors="replace").read()
    except Exception:
        return None

    info = {"source": "log",
            "tts_done": len(re.findall(r"第 \d+ 页合成完成", text)),
            "talking_done": len(re.findall(r"第 \d+ 页完成", text)),
            "total_pages": 0, "last_log_at": None, "stalled_seconds": None}

    mtime = os.path.getmtime(log_path)
    info["last_log_at"] = time.strftime("%H:%M:%S", time.localtime(mtime))
    info["stalled_seconds"] = int(max(0, time.time() - mtime))

    if "数字人与幻灯片合成完成" in text or "subtitle_render] 完成" in text:
        info["phase"] = "已完成"
    elif info["talking_done"]:
        info["phase"] = "生成数字人"
    elif info["tts_done"]:
        info["phase"] = "合成语音"
    elif "start stage" in text:
        info["phase"] = "生成幻灯片与讲稿"
    else:
        info["phase"] = ""
    return info


def read_progress(job):
    """把进度换算成前端要的形状（可直接推给前端）。

    两个来源，**优先结构化**：
      1. `progress.jsonl` —— P1，流水线主动写，机器可读
      2. `run.log` + 正则   —— P0 的兜底，零改造但脆
    终态（完成/失败/已取消）一律以**任务状态**为准，不从日志文本猜。
    """
    job_dir = _job_dir(job["id"])
    info = {
        "stage": job.get("stage", 0),
        "phase": job.get("phase", "等待中"),
        "tts_done": 0, "talking_done": 0, "total_pages": 0,
        "last_log_at": None, "stalled_seconds": None,
        "tail": _read_tail(job_dir),
        "source": "none",
    }

    found = _progress_from_jsonl(job, job_dir) or _progress_from_log(job, job_dir)
    if found:
        for key in ("tts_done", "talking_done", "total_pages"):
            if found.get(key):
                info[key] = found[key]
        if found.get("phase"):
            info["phase"] = found["phase"]
        info["source"] = found.get("source", info["source"])
        if found.get("last_log_at"):
            info["last_log_at"] = found["last_log_at"]
        if found.get("stalled_seconds") is not None:
            info["stalled_seconds"] = found["stalled_seconds"]

    # 总页数兜底：两个来源都没给出时，从渲染出的 PDF / 幻灯片图数推断
    if not info["total_pages"]:
        info["total_pages"] = _total_pages(job_dir)

    status = job.get("status")
    if status in ("done", "failed", "canceled"):
        info["phase"] = {"done": "已完成", "failed": "失败",
                         "canceled": "已取消"}[status]
    return info


# ──────────────────────────────────────────────────────────────
# 硬件探测 + 耗时标定
#
# 界面顶部要显示本机配置，页脚要"按配置估算时长"。
# 估算这件事很容易变成拍脑袋，所以这里把**依据**一起回报给前端：
# 标定值全部来自本机两次真实运行的 stage 计时（2026-09-18）。
# ──────────────────────────────────────────────────────────────

#   gkk_en 15 页（英文讲稿）: TTS 348.8 + 数字人 327.7 + 光标 2.6 + 合片 77.5 + 讲稿 111.3 = 868 s
#   gkk_zh 11 页（中文讲稿）: TTS 479.9 + 数字人 363.6 + 光标 17.2 + 合片 73.0 + 讲稿 0(复用) = 934 s
#
# 拆成「每页」+「固定」两项后，这组常数能同时复现上面两次实测：
#   15×45.1 + 85 + 110 = 871 s (14.5 min)  ← 实际 868 s
#   11×76.7 + 85      = 929 s (15.5 min)  ← 实际 934 s
# 中文每页明显更慢（TTS 43.6s/页 vs 英文 23.3s/页），而且音频更长还会连带拖慢数字人。
CALIBRATION = {
    "en_per_slide_s": 45.1,
    "zh_per_slide_s": 76.7,
    "fixed_s": 85.0,            # 光标 + 合片
    "script_api_s": 110.0,      # 讲稿生成（唯一付费步骤；--reuse_script 时为 0）
    "chars_per_slide": 280,     # 实测：正文 3540 字符 → 11~15 页
    "anchored_on": "RTX 5070 Ti 16GB",
    "note": "标定自本机两次真实运行；换硬件只做粗略折算，不是精确预测。",
}


def _cpu_name():
    if os.name == "nt":
        try:
            import winreg
            with winreg.OpenKey(
                    winreg.HKEY_LOCAL_MACHINE,
                    r"HARDWARE\DESCRIPTION\System\CentralProcessor\0") as key:
                return str(winreg.QueryValueEx(key, "ProcessorNameString")[0]).strip()
        except Exception:
            pass
    else:
        try:
            with open("/proc/cpuinfo", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if line.lower().startswith("model name"):
                        return line.split(":", 1)[1].strip()
        except Exception:
            pass
    return platform.processor() or platform.machine() or "未知"


def _cpu_cores():
    physical = logical = None
    try:
        import psutil
        physical = psutil.cpu_count(logical=False)
        logical = psutil.cpu_count(logical=True)
    except Exception:
        pass
    return {"physical": physical, "logical": logical or os.cpu_count()}


def _memory():
    try:
        import psutil
        vm = psutil.virtual_memory()
        return {"total_gb": round(vm.total / 2 ** 30, 1),
                "available_gb": round(vm.available / 2 ** 30, 1)}
    except Exception:
        return {}


def _gpus():
    try:
        out = subprocess.run(
            ["nvidia-smi",
             "--query-gpu=name,memory.total,memory.used,utilization.gpu,driver_version",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=15).stdout.strip()
    except Exception:
        return []
    gpus = []
    for line in out.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 5:
            continue
        try:
            gpus.append({
                "name": parts[0],
                "vram_total_mb": int(float(parts[1])),
                "vram_used_mb": int(float(parts[2])),
                "util_percent": int(float(parts[3])),
                "driver": parts[4],
            })
        except ValueError:
            continue
    return gpus


def _perf_factor(vram_total_mb):
    """按显存档位粗略折算耗时倍率（标定机 16GB = 1.0）。

    ⚠️ 这是**启发式**，不是测出来的：本机只有一块 16GB 卡，
    没有别的硬件可标定。它只用来把"15~25 分钟"这种空泛区间
    变成"按当前配置约 X 分钟"，界面上必须标明是粗估。
    """
    if not vram_total_mb:
        return None
    if vram_total_mb >= 20000:
        return 0.75
    if vram_total_mb >= 15000:
        return 1.0
    if vram_total_mb >= 11000:
        return 1.3
    return 1.8


def system_info():
    gpus = _gpus()
    vram = gpus[0]["vram_total_mb"] if gpus else None
    return {
        "os": "{} {}".format(platform.system(), platform.release()),
        "python": platform.python_version(),
        "cpu": dict(_cpu_cores(), name=_cpu_name()),
        "memory": _memory(),
        "gpus": gpus,
        "perf_factor": _perf_factor(vram),
        "calibration": CALIBRATION,
    }


def gpu_status():
    """显存占用 —— 用于提示"有别的任务在跑"。"""
    gpus = _gpus()
    if not gpus:
        return "（未检测到 GPU）"
    g = gpus[0]
    return "{} %, {} MiB, {} MiB".format(
        g["util_percent"], g["vram_used_mb"], g["vram_total_mb"])


# ──────────────────────────────────────────────────────────────
# 串行队列
# ──────────────────────────────────────────────────────────────
_QUEUE: asyncio.Queue = None
_WORKER: asyncio.Task = None
_CURRENT = {"job_id": None, "proc": None}


def _build_argv(job):
    """把任务配置翻译成流水线参数。

    ⚠️ `--if_tree_search False` 是**必须**的（默认值 True 会 NameError），
    所以它写死在这里，不从请求里读。
    """
    cfg = job["config"]
    argv = [
        VENV_PY, "-u", "pipeline.py",
        "--stage", json.dumps(cfg.get("stages", ["1", "2", "3"])),
        # result_dir 允许被显式指定：续跑时**直接复用原任务的 result 目录**，
        # 这样 stage 1 已生成的幻灯片与讲稿就地重用，不需要复制几 GB 的产物。
        "--result_dir", cfg.get("result_dir") or os.path.join(_job_dir(job["id"]), "result"),
        "--paper_latex_root", job["paper_latex_root"],
        "--ref_img", job["ref_img"],
        "--slide_lang", cfg["slide_lang"],
        "--script_lang", cfg["script_lang"],
        "--paper_type", cfg["paper_type"],
        "--talking_profile", cfg.get("talking_profile", "musetalk-local"),
        "--gpu_list", "0",
        "--if_tree_search", "False",
    ]
    if cfg.get("reuse_script"):
        argv += ["--reuse_script", "True"]
    return argv


def _log_line(log, message):
    """同时写 stdout 与 run.log。

    ⚠️ 为什么必须显式写 run.log：`_run_job` 里只有**子进程**的 stdout 被重定向到
    run.log，**父进程的 print 不会进去**。而 run.log 正是界面「日志尾巴」与
    「失败原因」的数据源 —— 只 print 的话，失败原因在界面上会凭空消失，
    使用者只看到"任务失败"却不知道原因（实测踩过：前置步骤失败时就是这样）。
    """
    print(message)
    try:
        log.write(message + "\n")
        log.flush()
    except Exception:
        pass


async def _prepare_latex(job, job_dir, log, env):
    """PDF / Word 输入的前置步骤：先把正文转成 LaTeX 工程（含 main.tex）。

    **为什么必须有它**：流水线的入口是「含 main.tex 的目录」
    （``--paper_latex_root``），而使用者的输入是 PDF / Word —— 这是整条链路上
    原本缺失的一环，不补上的话 stage 1 会因为找不到 main.tex 而直接失败。

    这一步要一次 LLM 调用（实测约 1 万 token），是**唯一无法本地完成**的一步；
    正文提取（paper_ingest）本身是纯本地的，包在 paper_struct 内部。
    已存在 main.tex 时直接跳过，便于重跑与续跑。

    返回 True 表示可以继续跑流水线。
    """
    latex_root = job.get("paper_latex_root") or os.path.join(job_dir, "latex_proj")
    main_tex = os.path.join(latex_root, "main.tex")
    if os.path.isfile(main_tex):
        _log_line(log, "[struct] main.tex 已存在，跳过结构化 -> {}".format(main_tex))
        return True

    source = job.get("source_path")
    if not source or not os.path.isfile(source):
        _log_line(log, "[struct] 需要结构化，但源文件不可用：{}".format(source))
        _log_line(log, "[struct] （续跑旧任务时若素材已被清理，请重新上传论文）")
        return False

    argv = [VENV_PY, "-u", os.path.join(SCRIPTS, "paper_struct.py"),
            source, "--out", job_dir]
    _log_line(log, "[struct] 正在把 {} 转成 LaTeX 工程（一次 API 调用，约 1~2 分钟）".format(
        os.path.basename(source)))
    proc = await asyncio.create_subprocess_exec(
        *argv, cwd=REPO_ROOT, stdout=log, stderr=asyncio.subprocess.STDOUT, env=env)
    _CURRENT["proc"] = proc
    _CURRENT["job_id"] = job["id"]
    try:
        code = await proc.wait()
    finally:
        _CURRENT["proc"] = None
        _CURRENT["job_id"] = None

    if code != 0 or not os.path.isfile(main_tex):
        _log_line(log, "[struct] 结构化失败（退出码 {}），main.tex 未生成".format(code))
        return False
    _log_line(log, "[struct] OK -> {}".format(main_tex))
    return True


async def _run_job(job):
    """在子进程里跑流水线，日志落盘供进度解析。"""
    job_id = job["id"]
    job_dir = _job_dir(job_id)
    log_path = os.path.join(job_dir, "run.log")
    argv = _build_argv(job)

    job["status"] = "running"
    job["phase"] = "启动中"
    job["argv"] = argv
    _save_job(job)

    env = dict(os.environ)
    # P1：告诉流水线把结构化进度写到哪里。未设置该环境变量时，
    # 流水线**不生成任何额外文件**（命令行直接跑 pipeline.py 行为完全不变）。
    env["PAPER2VIDEO_PROGRESS_FILE"] = os.path.join(job_dir, "progress.jsonl")
    if _SESSION_KEYS["api_key"]:
        env["OPENAI_API_KEY"] = _SESSION_KEYS["api_key"]
        env["OPENAI_COMPATIBLE_MODEL_API_KEY"] = _SESSION_KEYS["api_key"]
    if _SESSION_KEYS["api_base"]:
        env["OPENAI_API_BASE"] = _SESSION_KEYS["api_base"]
        env["OPENAI_BASE_URL"] = _SESSION_KEYS["api_base"]
        env["OPENAI_COMPATIBLE_MODEL_API_BASE"] = _SESSION_KEYS["api_base"]

    started = time.time()
    with io.open(log_path, "w", encoding="utf-8", errors="replace") as log:
        log.write("### 命令 {}\n".format(" ".join(argv)))
        log.flush()

        # 前置：PDF / Word 输入先转成 LaTeX 工程（见 _prepare_latex 的说明）
        prepared = True
        if job.get("needs_struct"):
            prepared = await _prepare_latex(job, job_dir, log, env)

        if not prepared:
            # 前置步骤失败：不跑流水线，但仍走下面的收尾写入，
            # 让任务落到 failed 且带上 exit_code（否则状态会一直停在 running）。
            code = 1
        else:
            # 直接传 argv 列表，不经 shell —— 中文路径与转义问题都由此规避
            proc = await asyncio.create_subprocess_exec(
                *argv, cwd=P2V_SRC, stdout=log, stderr=asyncio.subprocess.STDOUT,
                env=env)
            _CURRENT["proc"] = proc
            _CURRENT["job_id"] = job_id      # 供 /api/system/gpu 报告"正在跑哪个任务"
            try:
                code = await proc.wait()
            finally:
                _CURRENT["proc"] = None
                _CURRENT["job_id"] = None

    # ⚠️ 必须走 _update_job：它会先重新读盘再改，避免用旧快照覆盖
    #    取消接口刚写下的 status=canceled（实测踩过，见 _JOB_LOCK 处的注释）。
    async with _JOB_LOCK:
        final = _load_job(job_id)
        final["exit_code"] = code
        final["elapsed_min"] = round((time.time() - started) / 60, 1)
        if final.get("status") == "canceled":
            # 取消是使用者的明确意图，不该被退出码改写；
            # 但退出码与耗时仍要记下来 —— 排查时有用，且能证明收尾确实执行过。
            final["phase"] = "已取消"
        else:
            final["status"] = "done" if code == 0 else "failed"
            final["phase"] = "已完成" if code == 0 else "失败"
        _save_job(final)


async def _worker_loop():
    while True:
        job_id = await _QUEUE.get()
        try:
            job = _load_job(job_id)
            if job.get("status") == "canceled":
                continue
            await _run_job(job)
        except Exception as exc:                      # 崩溃不能带走整个 worker
            try:
                await _update_job(job_id, status="failed",
                                  error="{}: {}".format(type(exc).__name__, exc))
            except Exception:
                pass
        finally:
            _QUEUE.task_done()


# ──────────────────────────────────────────────────────────────
# 路由
# ──────────────────────────────────────────────────────────────
@app.on_event("startup")
async def _startup():
    global _QUEUE, _WORKER
    _QUEUE = asyncio.Queue()
    _WORKER = asyncio.create_task(_worker_loop())


@app.get("/api/options")
async def api_options():
    """把可选档位下发 —— 前端不硬编码，避免两处维护。"""
    import language_config as lc

    presets = [{"value": k, "label": v["desc"],
                "slide_lang": v["slide_lang"], "script_lang": v["script_lang"]}
               for k, v in lc.VIDEO_PRESETS.items()]
    paper_types = [{"value": k, "label": v["desc"]}
                   for k, v in lc.PAPER_TYPES.items()]
    return {
        "presets": presets,
        "slide_langs": [{"value": k, "label": v["desc"]} for k, v in lc.SLIDE_LANGS.items()],
        "script_langs": [{"value": k, "label": v["desc"]} for k, v in lc.SCRIPT_LANGS.items()],
        "paper_types": paper_types,
        "accept_formats": [".tex", ".zip", ".docx", ".doc", ".pdf"],
        # 明确告知前端：这个开关不存在，不要试图暴露
        "hidden_flags": {"if_tree_search": "写死 False（默认值 True 会 NameError）"},
    }


@app.post("/api/key/verify")
async def api_key_verify(payload: dict):
    """提交前自检：区分「欠费 / 鉴权失败 / 网络不通」。

    为什么要专门做这个：DeepSeek 余额耗尽时**不返回明确错误码**，
    而是返回空响应或截断内容 —— 症状与"网关抖动"完全一样，
    之前就据此误判过一轮。所以这里必须把三类失败分开报。
    """
    import pv_env  # noqa: F401
    from openai import OpenAI

    api_key = (payload.get("api_key") or _SESSION_KEYS["api_key"]).strip()
    api_base = (payload.get("api_base") or _SESSION_KEYS["api_base"]).strip()
    if not api_key:
        return {"ok": False, "kind": "missing", "message": "未填写 API Key"}

    client = OpenAI(api_key=api_key, base_url=api_base or None, timeout=30)
    try:
        resp = client.chat.completions.create(
            model=payload.get("model") or "deepseek-flash",
            messages=[{"role": "user", "content": "ping"}],
            max_tokens=8, temperature=0)
        usage = getattr(resp, "usage", None)
        # 校验通过就记进进程内存 —— 这样真正开跑时 _run_job 才能把它注入子进程。
        # ⚠️ 原先只校验不保存，于是注入那段代码**永远拿不到使用者填的 key**，
        #    实际用的是 .env 里那把：本地因为 .env 有 key 所以"看着能跑"，
        #    换到没有 .env 的服务器就会静默失败。
        # 注意这是**进程级**的：多人共用同一服务时会互相覆盖，
        # 学校服务器阶段需要把凭据挂到任务上做隔离（方案文档已列）。
        _SESSION_KEYS["api_key"] = api_key
        _SESSION_KEYS["api_base"] = api_base
        return {"ok": True, "kind": "ok",
                "message": "连接正常",
                "usage": {"total_tokens": getattr(usage, "total_tokens", None)}}
    except Exception as exc:
        text = "{}".format(exc)
        low = text.lower()
        if any(k in low for k in ("insufficient", "balance", "quota", "欠费", "余额")):
            kind, msg = "insufficient_balance", "余额不足 —— 请先充值"
        elif any(k in low for k in ("401", "unauthorized", "invalid api key", "authentication")):
            kind, msg = "auth", "鉴权失败 —— Key 不正确或已失效"
        elif any(k in low for k in ("timeout", "connection", "resolve", "network")):
            kind, msg = "network", "网络不通 —— 检查 API Base 与代理"
        else:
            kind, msg = "unknown", "请求失败"
        return {"ok": False, "kind": kind, "message": msg, "detail": text[:300]}


@app.post("/api/ingest")
async def api_ingest(paper: UploadFile = File(...)):
    """上传论文 → 提取正文 → 返回预览。

    这是「转换预览」这一步的后端：把 main.tex 的章节结构 + 正文开头
    给使用者确认，确认无误再开跑 —— 因为转换错了下游无法补救。
    """
    tmp_id = "ingest_" + uuid.uuid4().hex[:8]
    work = os.path.join(JOBS_ROOT, tmp_id)
    os.makedirs(os.path.join(work, "input"), exist_ok=True)

    safe_name = os.path.basename(paper.filename or "upload.bin")
    src = os.path.join(work, "input", safe_name)
    with io.open(src, "wb") as handle:
        while True:                                   # 流式落盘，不整块读进内存
            chunk = await paper.read(1 << 20)
            if not chunk:
                break
            handle.write(chunk)

    import paper_ingest as pi
    try:
        text, meta = pi.ingest(src, os.path.join(work, "ingest"))
    except Exception as exc:
        # 失败就把临时目录删掉：否则每次失败都在 jobs/ 里留一个空壳，
        # 使用者（和后续排查的人）会以为"上传过但没结果"（实测踩过）。
        shutil.rmtree(work, ignore_errors=True)
        return JSONResponse(status_code=400, content={
            "ok": False,
            "error": "{}: {}".format(type(exc).__name__, exc),
            "hint": "支持 .tex / .zip / .docx / .doc / .pdf",
        })

    return {
        "ok": True,
        "ingest_id": tmp_id,
        "source_path": src,
        "detected_format": meta.get("detected_format"),
        "needs_llm": meta.get("needs_llm", True),
        "chars": meta.get("chars", len(text or "")),
        "docx_headings": meta.get("headings"),
        "lossy": meta.get("lossy", False),
        "hint": meta.get("hint"),
        "preview_head": (text or "")[:1500],
        "preview_tail": (text or "")[-500:] if text else "",
        "latex_root": meta.get("latex_root"),
    }


@app.post("/api/portrait")
async def api_portrait(paper: UploadFile = File(...)):
    """上传人像图（数字人环节的输入）。

    **为什么必须与 `/api/ingest` 分开**：那个接口做的是"论文 → 正文提取"，
    拿图片进去必然报「无法识别的文件格式」。而前端原先正是复用它上传人像 ——
    结果是**人像永远传不上去、任务永远建不起来**，界面上只看到"人像图上传失败"。
    人像只需要"存下来 + 基本校验"，完全不需要解析。

    校验做到什么程度：只验**是不是图片**（按文件头，不看扩展名）与尺寸合理性。
    **不做人脸检测** —— 本机 OpenCV 5 已移除级联分类器且不带数据文件，
    要做得下载模型，而学校服务器可能离线。所以返回里写明"未做检测"，
    并列出人工确认要点，不给虚假的安全感。
    """
    tmp_id = "portrait_" + uuid.uuid4().hex[:8]
    work = os.path.join(JOBS_ROOT, tmp_id)
    os.makedirs(os.path.join(work, "input"), exist_ok=True)

    safe_name = os.path.basename(paper.filename or "portrait.png")
    dest = os.path.join(work, "input", safe_name)
    with io.open(dest, "wb") as handle:
        while True:                                   # 流式落盘，不整块读进内存
            chunk = await paper.read(1 << 20)
            if not chunk:
                break
            handle.write(chunk)

    fmt = _sniff_image(dest)
    if fmt is None:
        shutil.rmtree(work, ignore_errors=True)       # 失败不留空壳目录
        return JSONResponse(status_code=400, content={
            "ok": False,
            "error": "这不是一张能识别的图片（按文件头判断，不看扩展名）",
            "hint": "支持 PNG / JPEG / WEBP / BMP",
        })

    warnings = []
    width = height = None
    try:
        from PIL import Image
        with Image.open(dest) as image:
            width, height = image.size
    except Exception as exc:
        warnings.append("读取尺寸失败（{}），仍可尝试使用".format(type(exc).__name__))

    if width and height:
        short_side = min(width, height)
        long_side = max(width, height)
        if short_side < 256:
            warnings.append(
                "短边仅 {} 像素：数字人只重绘 256×256 的人脸区域，这个尺寸会明显模糊"
                .format(short_side))
        elif short_side < 512:
            warnings.append("短边 {} 像素偏小，建议 512 以上".format(short_side))
        if long_side > 4096:
            warnings.append("分辨率 {}×{} 偏大（白耗显存），建议不超过 4096"
                            .format(width, height))

    return {
        "ok": True,
        "portrait_path": dest,
        "format": fmt,
        "width": width,
        "height": height,
        "size_mb": round(os.path.getsize(dest) / 2 ** 20, 2),
        "warnings": warnings,
        "note": "未做人脸检测（本机 OpenCV 已移除级联分类器）。请自行确认："
                "正脸、单人、无遮挡；侧脸或多人会让数字人环节失败或效果很差。",
    }


@app.post("/api/jobs")
async def api_create_job(payload: dict):
    """创建任务 → 入队。同一时刻只会有一个在跑（单卡）。"""
    ingest_id = payload.get("ingest_id")
    work = os.path.join(JOBS_ROOT, ingest_id or "")
    if not ingest_id or not os.path.isdir(work):
        raise HTTPException(status_code=400, detail="先上传论文（缺少有效的 ingest_id）")

    # 创建任务时也接受凭据（使用者可能没点"自检"就直接开跑）。
    # 只放进程内存，不落盘。
    if payload.get("api_key"):
        _SESSION_KEYS["api_key"] = str(payload["api_key"]).strip()
    if payload.get("api_base"):
        _SESSION_KEYS["api_base"] = str(payload["api_base"]).strip()

    # 人像与音色也是上传来的；没人像就直接拒绝（数字人环节必然失败）
    portrait = payload.get("portrait_path")
    if not portrait or not os.path.isfile(portrait):
        raise HTTPException(status_code=400, detail="缺少人像图")

    job_id = "job_" + time.strftime("%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:4]
    job_dir = _job_dir(job_id)
    os.makedirs(job_dir, exist_ok=True)
    shutil.copytree(os.path.join(work, "ingest"), os.path.join(job_dir, "ingest"),
                    dirs_exist_ok=True)

    meta_path = os.path.join(job_dir, "ingest", "paper_text.md")
    needs_llm = os.path.isfile(meta_path)

    job = {
        "id": job_id,
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "status": "queued",
        "phase": "排队中",
        "queue_ahead": _QUEUE.qsize(),
        "config": payload.get("config", {}),
        "ref_img": portrait,
        "paper_latex_root": payload.get("paper_latex_root") or os.path.join(job_dir, "latex_proj"),
        "needs_struct": needs_llm,
        # 原始上传文件的路径。needs_struct 时前置步骤要用它做结构化；
        # 放在这里而不是去 jobs/<ingest_id>/input 里找，是因为 ingest 目录
        # 将来可能被清理，而任务自己记着更可靠。
        "source_path": payload.get("source_path"),
    }
    _save_job(job)
    await _QUEUE.put(job_id)
    return {"ok": True, "job_id": job_id, "queue_ahead": _QUEUE.qsize(),
            "queued": True}


@app.post("/api/jobs/{job_id}/rerun")
async def api_rerun_job(job_id: str, payload: dict):
    """从已有任务续跑（默认只重跑 stage 2/3，并复用已有讲稿）。

    为什么这是**省钱主力**：讲稿生成是整条链路里唯一必须联网付费的一步，
    而它占约 76% 的 token（要把全部幻灯片图喂给 VLM）。只重跑 stage 2/3 且
    `--reuse_script` 复用已有讲稿 ⇒ 这次调用**不产生 API 费用**
    （实测：gkk_zh 的补跑就是 0 费用）。

    实现上**不复制产物**：新任务直接指向原任务的 result 目录，
    于是 stage 1 已生成的幻灯片与讲稿就地重用（动辄几百 MB~GB，复制没必要）。
    """
    src_job = _load_job(job_id)
    src_result = os.path.join(_job_dir(job_id), "result")
    if not os.path.isdir(src_result):
        raise HTTPException(status_code=400,
                            detail="原任务没有 result 目录，无法续跑（可能从未开始）")

    portrait = payload.get("portrait_path") or src_job.get("ref_img")
    if not portrait or not os.path.isfile(portrait):
        raise HTTPException(
            status_code=400,
            detail="续跑需要人像图，但原任务的人像已不在（素材可能被清理），请重新上传")

    config = dict(src_job.get("config") or {})
    config.update({
        "stages": payload.get("stages") or ["2", "3"],
        "reuse_script": payload.get("reuse_script", True),
        "result_dir": src_result,
    })

    new_id = "job_" + time.strftime("%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:4]
    new_dir = _job_dir(new_id)
    os.makedirs(new_dir, exist_ok=True)
    src_ingest = os.path.join(_job_dir(job_id), "ingest")
    if os.path.isdir(src_ingest):                 # ingest 很小（正文），复制一份
        shutil.copytree(src_ingest, os.path.join(new_dir, "ingest"),
                        dirs_exist_ok=True)

    job = {
        "id": new_id,
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "status": "queued",
        "phase": "排队中",
        "queue_ahead": _QUEUE.qsize(),
        "config": config,
        "ref_img": portrait,
        "paper_latex_root": src_job.get("paper_latex_root"),
        # 续跑时 LaTeX 工程一定已经存在（stage 1 跑过），不要再触发一次结构化
        "needs_struct": False,
        "source_path": src_job.get("source_path"),
        "resumed_from": job_id,
    }
    _save_job(job)
    await _QUEUE.put(new_id)
    return {"ok": True, "job_id": new_id, "resumed_from": job_id,
            "stages": config["stages"], "reuse_script": config["reuse_script"],
            "queue_ahead": _QUEUE.qsize()}


@app.get("/api/jobs")
async def api_list_jobs():
    return {"jobs": _list_jobs()}


@app.get("/api/jobs/{job_id}")
async def api_get_job(job_id: str):
    job = _load_job(job_id)
    job["progress"] = read_progress(job)
    return job


@app.get("/api/jobs/{job_id}/events")
async def api_job_events(job_id: str, request: Request):
    """SSE 进度流。

    长任务（15~25 分钟）必须用流式推送；页面刷新后重连也能接着看，
    状态一律以后端为准，不依赖前端内存。
    """
    async def gen():
        last = None
        while True:
            if await request.is_disconnected():
                break
            try:
                job = _load_job(job_id)
            except HTTPException:
                yield "event: error\ndata: {}\n\n".format(
                    json.dumps({"error": "任务不存在"}, ensure_ascii=False))
                break
            payload = {
                "status": job.get("status"),
                "phase": job.get("phase"),
                "progress": read_progress(job),
                "exit_code": job.get("exit_code"),
                "elapsed_min": job.get("elapsed_min"),
            }
            body = json.dumps(payload, ensure_ascii=False)
            if body != last:                       # 只在变化时推，减少噪音
                yield "data: {}\n\n".format(body)
                last = body
            if job.get("status") in ("done", "failed", "canceled"):
                break
            await asyncio.sleep(2)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


@app.post("/api/jobs/{job_id}/cancel")
async def api_cancel_job(job_id: str):
    """取消：杀子进程 —— 这也是"用子进程"的一个直接好处。"""
    job = _load_job(job_id)
    proc = _CURRENT.get("proc")
    if job.get("status") == "running" and proc is not None:
        try:
            proc.kill()
            await proc.wait()
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
    # 只改这两个字段。**不要**拿刚 load 的整份 job 去 save ——
    # 那份快照是在 kill 之前读的，而 _run_job 的收尾逻辑会在进程退出后
    # 写入 exit_code / elapsed_min；整份覆盖会把它们抹掉（实测踩过）。
    await _update_job(job_id, status="canceled", phase="已取消")
    return {"ok": True}


@app.get("/api/jobs/{job_id}/artifacts")
async def api_artifacts(job_id: str):
    job = _load_job(job_id)
    base = os.path.join(_job_dir(job_id), "result")
    items = []
    if os.path.isdir(base):
        for name in sorted(os.listdir(base)):
            path = os.path.join(base, name)
            if os.path.isfile(path):
                items.append({
                    "name": name,
                    "size_mb": round(os.path.getsize(path) / 2 ** 20, 2),
                    "kind": "video" if name.endswith(".mp4") else "file",
                })
    return {"job": job_id, "artifacts": items}


@app.get("/api/jobs/{job_id}/pages")
async def api_job_pages(job_id: str):
    """逐页素材浏览：把同一页的幻灯片图 / 音频 / 数字人视频 / 讲稿索引到一起。

    为什么必须在服务端拼：这些产物的命名规则**彼此不一致** ——
    幻灯片是 `slide_imgs/1.png`（从 1 开始）、音频是 `audio/0.wav`（从 0 开始）、
    数字人还多一层「参考图主文件名」的目录。前端不该知道这些细节，
    否则上游一改命名就会**静默取不到**（界面上只是空着，看不出是 bug）。

    这里只做**索引**，文件本身仍走 /files/ 端点（FileResponse 自带 Range，
    视频才能拖进度条、图片才能按需加载）。
    """
    _load_job(job_id)                                   # 任务不存在则 404
    base = os.path.join(_job_dir(job_id), "result")

    def _listing(sub, exts):
        directory = os.path.join(base, sub)
        if not os.path.isdir(directory):
            return {}
        found = {}
        for name in os.listdir(directory):
            stem, ext = os.path.splitext(name)
            if ext.lower() in exts and stem.isdigit():
                found[int(stem)] = "{}/{}".format(sub, name)
        return found

    slides = _listing("slide_imgs", {".png", ".jpg", ".jpeg"})
    audios = _listing("audio", {".wav", ".mp3"})

    # 数字人：talking_<backend>/<音频序号>/<参考图主文件名>/merge_video.mp4
    # （merge_talking.py 写死了这个层级，所以这里也按它找；顺带兼容直接放在页目录下）
    talking = {}
    if os.path.isdir(base):
        for name in sorted(os.listdir(base)):
            if not name.startswith("talking_"):
                continue
            top = os.path.join(base, name)
            if not os.path.isdir(top):
                continue
            for page in os.listdir(top):
                if not page.isdigit():
                    continue
                page_dir = os.path.join(top, page)
                rel = None
                for sub in sorted(os.listdir(page_dir)):
                    candidate = os.path.join(page_dir, sub, "merge_video.mp4")
                    if os.path.isfile(candidate):
                        rel = "{}/{}/{}/merge_video.mp4".format(name, page, sub)
                        break
                if rel is None and os.path.isfile(
                        os.path.join(page_dir, "merge_video.mp4")):
                    rel = "{}/{}/merge_video.mp4".format(name, page)
                if rel:
                    talking[int(page)] = rel

    # 讲稿：subtitle_w_cursor.txt 以 ### 分页，页内每行是「句子 | 光标短语」
    script = {}
    script_path = os.path.join(base, "subtitle_w_cursor.txt")
    if os.path.isfile(script_path):
        try:
            raw = io.open(script_path, encoding="utf-8", errors="replace").read()
            chunks = [c for c in raw.split("###") if c.strip()]
            for idx, chunk in enumerate(chunks):
                # 即使该页没有解析出行也要占位，否则后面几页的讲稿会整体错位一页
                script[idx] = [ln.split("|", 1)[0].strip()
                               for ln in chunk.splitlines() if "|" in ln]
        except Exception:
            pass

    total = max([len(slides)] + [k + 1 for k in audios] + [0])
    pages = []
    for i in range(total):
        pages.append({
            "page": i + 1,                  # 界面统一从 1 开始
            "slide": slides.get(i + 1),
            "audio": audios.get(i),
            "talking": talking.get(i),
            "script": script.get(i, []),
        })
    return {
        "job": job_id,
        "total": total,
        "slides_pdf": "slides_rendered.pdf" if os.path.isfile(
            os.path.join(base, "slides_rendered.pdf")) else None,
        "script_txt": "subtitle_w_cursor.txt" if os.path.isfile(script_path) else None,
        "cursor_json": "cursor.json" if os.path.isfile(
            os.path.join(base, "cursor.json")) else None,
        "pages": pages,
    }


@app.get("/api/jobs/{job_id}/files/{rel_path:path}")
async def api_job_file(job_id: str, rel_path: str):
    """产物文件服务。FileResponse 自带 Range 支持，视频才能拖进度条。"""
    root = os.path.join(_job_dir(job_id), "result")
    target = os.path.realpath(os.path.join(root, rel_path))
    if not target.startswith(os.path.realpath(root)):     # 防目录穿越
        raise HTTPException(status_code=403, detail="越权路径")
    if not os.path.isfile(target):
        raise HTTPException(status_code=404, detail="文件不存在")
    return FileResponse(target)


@app.get("/api/system/gpu")
async def api_gpu():
    return {"gpu": gpu_status(), "running_job": _CURRENT.get("job_id")}


@app.get("/api/system/info")
async def api_system_info():
    """本机配置 + 耗时标定值。

    前端用它做两件事：顶部显示硬件、页脚按当前配置给出预计时长。
    把标定值也一并下发（而不是写死在前端），是为了让"估算依据"只有一处来源。
    """
    return system_info()


# 前端构建产物（vite build 输出）。没有就退化成纯 API —— 开发时用 vite dev。
_STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
if os.path.isdir(_STATIC):
    app.mount("/", StaticFiles(directory=_STATIC, html=True), name="static")


# 平台判定放在模块级，方便在测试里替换以验证另一平台的分支。
_IS_WINDOWS = os.name == "nt"


def _probe_options():
    """探测端口时该设的 socket 选项。**平台相关，且两个平台的要求正好相反。**

    这里踩过一个坑，值得写清楚：

    - **Windows**：`SO_REUSEADDR` 的语义与 POSIX **相反** —— 它不是"允许复用 TIME_WAIT
      地址"，而是允许 bind 到一个**已被别的进程占用**的端口（相当于抢占）。
      拿它做探测的结果是**永远报"空闲"**，预检形同虚设（实测：端口明明被占，
      预检却放行，一路走到 uvicorn 才抛 WinError 10048）。
      所以 Windows 上**必须不设**该选项；再加 `SO_EXCLUSIVEADDRUSE` 明确声明独占，
      只要端口还有别人占着，bind 就失败。

    - **POSIX**：`SO_REUSEADDR` 才是正规用法（允许复用 TIME_WAIT 的地址），
      而且**服务端本身就会用它**（asyncio 的 `create_server` 在 POSIX 上
      `reuse_address` 默认为真）。若探测时不设，**刚关掉服务就重启**会把残留的
      TIME_WAIT 误判成"被占用"，把一个本来能正常启动的端口判成不可用 ——
      也就是把 Windows 的 bug 换成 Linux 的 bug。
    """
    import socket
    opts = []
    if _IS_WINDOWS:
        exclusive = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)
        if exclusive is not None:
            opts.append((socket.SOL_SOCKET, exclusive, 1))
    else:
        opts.append((socket.SOL_SOCKET, socket.SO_REUSEADDR, 1))
    return opts


def _try_bind(host, port):
    """按**与平台匹配的语义**试绑一次，成功返回 True。

    判据就是"本服务能不能起来"，不试图去猜别的进程的状态 ——
    猜端口占用很容易产生假阳性（例如别的进程占的是 0.0.0.0 而我们绑 127.0.0.1，
    或者只剩 TIME_WAIT），而假阳性会把一个能用的端口拦下来。
    """
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        for level, name, value in _probe_options():
            sock.setsockopt(level, name, value)
        try:
            sock.bind((host, port))
        except OSError:
            return False
    return True


def _listener_pid(port):
    """LISTEN 在该端口上的进程 pid（仅用于把报错写得具体）；查不到返回 None。"""
    try:
        import psutil
    except ImportError:
        return None
    try:
        for conn in psutil.net_connections(kind="inet"):
            if conn.laddr and conn.laddr.port == port and conn.status == psutil.CONN_LISTEN:
                return conn.pid
    except Exception:
        return None
    return None


def _port_owner(port):
    """占用者的可读描述（进程名 / pid / 已运行时长）。拿不到就返回 None。"""
    pid = _listener_pid(port)
    if pid is None:
        return None
    try:
        import psutil
        proc = psutil.Process(pid)
        return "{} (pid={}, 已运行 {:.0f} 分钟)".format(
            proc.name(), pid, (time.time() - proc.create_time()) / 60)
    except Exception:
        return "pid={}".format(pid)


def _find_free_port(host, start, tries=20):
    for offset in range(tries):
        candidate = start + offset
        if _try_bind(host, candidate):
            return candidate
    return None


def _port_in_use(host, port):
    """本服务能否在这个端口上启动（True = 起不来）。"""
    return not _try_bind(host, port)


def main():
    import argparse

    import uvicorn

    parser = argparse.ArgumentParser(description="Paper2Video Web 服务")
    parser.add_argument("--host", default=os.environ.get("P2V_WEB_HOST", "127.0.0.1"),
                        help="监听地址，默认 127.0.0.1（只监听本机）")
    parser.add_argument("--port", type=int,
                        default=int(os.environ.get("P2V_WEB_PORT", "8848")),
                        help="监听端口，默认 8848")
    parser.add_argument("--auto-port", action="store_true",
                        help="端口被占时自动顺延选一个空闲端口，而不是退出")
    args = parser.parse_args()

    # ── 启动前先探端口。理由：uvicorn 抛出的原生错误是
    #    「[Errno 10048] 通常每个套接字地址只允许使用一次」——
    #    它既不告诉你是谁占着，也不告诉你怎么改，看着像代码坏了。
    #    这里提前拦下，并指出占用进程与解决办法。
    if _port_in_use(args.host, args.port):
        owner = _port_owner(args.port)
        print("端口 {} 已被占用{}。".format(
            args.port, "：" + owner if owner else ""))
        if args.auto_port:
            free = _find_free_port(args.host, args.port + 1)
            if free is None:
                print("从 {} 起试了 20 个端口都不可用，请手动指定 --port。".format(args.port + 1))
                raise SystemExit(2)
            print("已自动改用端口 {}。".format(free))
            args.port = free
        else:
            print("解决办法（任选其一）：")
            print("  1) 换端口：  python web/app.py --port 8849")
            print("  2) 自动顺延：python web/app.py --auto-port")
            print("  3) 结束占用进程后重试（上面的 pid 可用任务管理器结束，或：taskkill /PID <pid> /F）")
            raise SystemExit(2)

    print("Paper2Video Web → http://{}:{}".format(args.host, args.port))
    print("（只监听本机；如需局域网访问请自行评估风险后设置 --host 或 P2V_WEB_HOST）")
    if not os.path.isdir(_STATIC):
        print("提示：{} 不存在，当前只有 API。执行 `npm run build` 后重启即可看到界面。".format(
            os.path.relpath(_STATIC, os.path.dirname(os.path.abspath(__file__)))))
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
