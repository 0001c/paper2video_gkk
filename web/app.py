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


def read_progress(job):
    """把日志换算成结构化进度。返回 dict，可直接推给前端。"""
    log_path = os.path.join(_job_dir(job["id"]), "run.log")
    info = {
        "stage": job.get("stage", 0),
        "phase": job.get("phase", "等待中"),
        "tts_done": 0, "talking_done": 0, "total_pages": 0,
        "last_log_at": None, "stalled_seconds": None,
        "tail": [],
    }
    if not os.path.isfile(log_path):
        return info

    info["total_pages"] = _total_pages(_job_dir(job["id"]))
    try:
        text = io.open(log_path, encoding="utf-8", errors="replace").read()
    except Exception:
        return info

    info["tts_done"] = len(re.findall(r"第 \d+ 页合成完成", text))
    info["talking_done"] = len(re.findall(r"第 \d+ 页完成", text))
    info["tail"] = [ln.strip() for ln in text.splitlines() if ln.strip()][-6:]

    mtime = os.path.getmtime(log_path)
    info["last_log_at"] = time.strftime("%H:%M:%S", time.localtime(mtime))
    info["stalled_seconds"] = int(time.time() - mtime)

    # 阶段推断
    if "数字人与幻灯片合成完成" in text or "subtitle_render] 完成" in text:
        info["phase"] = "已完成"
    elif info["talking_done"]:
        info["phase"] = "生成数字人"
    elif info["tts_done"]:
        info["phase"] = "合成语音"
    elif "start stage" in text:
        info["phase"] = "生成幻灯片与讲稿"
    return info


def gpu_status():
    """显存占用 —— 用于提示"有别的任务在跑"。"""
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=utilization.gpu,memory.used,memory.total",
             "--format=csv,noheader"],
            capture_output=True, text=True, timeout=15).stdout.strip()
        return out
    except Exception as exc:
        return "（读取失败：{}）".format(type(exc).__name__)


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
        "--result_dir", os.path.join(_job_dir(job["id"]), "result"),
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


async def _run_job(job):
    """在子进程里跑流水线，日志落盘供进度解析。"""
    job_dir = _job_dir(job["id"])
    log_path = os.path.join(job_dir, "run.log")
    argv = _build_argv(job)

    job["status"] = "running"
    job["phase"] = "启动中"
    job["argv"] = argv
    _save_job(job)

    env = dict(os.environ)
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
        # 直接传 argv 列表，不经 shell —— 中文路径与转义问题都由此规避
        proc = await asyncio.create_subprocess_exec(
            *argv, cwd=P2V_SRC, stdout=log, stderr=asyncio.subprocess.STDOUT,
            env=env)
        _CURRENT["proc"] = proc
        code = await proc.wait()
        _CURRENT["proc"] = None

    job = _load_job(job["id"])
    job["exit_code"] = code
    job["elapsed_min"] = round((time.time() - started) / 60, 1)
    job["status"] = "done" if code == 0 else "failed"
    job["phase"] = "已完成" if code == 0 else "失败"
    _save_job(job)


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
                job = _load_job(job_id)
                job["status"] = "failed"
                job["error"] = "{}: {}".format(type(exc).__name__, exc)
                _save_job(job)
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


@app.post("/api/jobs")
async def api_create_job(payload: dict):
    """创建任务 → 入队。同一时刻只会有一个在跑（单卡）。"""
    ingest_id = payload.get("ingest_id")
    work = os.path.join(JOBS_ROOT, ingest_id or "")
    if not ingest_id or not os.path.isdir(work):
        raise HTTPException(status_code=400, detail="先上传论文（缺少有效的 ingest_id）")

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
    }
    _save_job(job)
    await _QUEUE.put(job_id)
    return {"ok": True, "job_id": job_id, "queue_ahead": _QUEUE.qsize(),
            "queued": True}


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
    job["status"] = "canceled"
    job["phase"] = "已取消"
    _save_job(job)
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
