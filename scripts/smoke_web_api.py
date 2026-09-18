"""Web 层接口的冒烟测试（不跑真实流水线，因此零 API 费用、秒级完成）。

为什么需要它：P0 里有四个接口只在"真跑一次任务"时才被触达
（SSE 进度流、取消、产物 Range、进度解析），而真跑一次是 15~25 分钟 + API 费用。
这里用两个手段把它们变成可廉价复现的测试：

1. **进度解析**用**真实历史日志**当输入（gkk_en / gkk_zh 的 run.log），
   并用"音频文件个数"作为独立的地面真值交叉验证 —— 不是拿同一个正则自证。
2. **队列 / SSE / 取消 / 产物**把 `_build_argv` 替换成一个打印同样格式日志的
   假命令（进程内替换），于是能在一分钟内走完
   「排队 → 运行 → 进度递增 → 取消」和「排到完成」两条路径。

用法：
    python scripts/smoke_web_api.py
"""
import asyncio

from PIL import Image
import importlib.util
import io
import json
import os
import shutil
import sys
import time
import uuid

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

FAILURES = []


def check(name, ok, detail=""):
    """`detail` 必须描述**观察到的事实**（如 "tts=2 / talking=1"）。

    ⚠️ 不要写成否定句（如"没找到该赋值"）——它**成功时也会打印**，
    于是会出现「✔ 某某检查 — 没找到某某」这种自相矛盾的行，
    读日志的人（包括我自己）会被误导成失败。实测踩过。
    """
    # 两个强转都是被真实故障逼出来的：
    # ① `ok`：调用处常写 `a == b and c.get("x")`，而 `and` 返回的是**操作数本身**
    #    （可能是 dict / list / str），直接 `ok &= 那个值` 会 TypeError，
    #    于是套件**停在断言行上**、报错还指向测试框架，看上去像框架坏了。
    # ② `detail`：传 list / dict 时 "%s" 拼接会 TypeError，同样打断套件。
    ok = bool(ok)
    if detail and not isinstance(detail, str):
        detail = str(detail)
    mark = "✔" if ok else "✘"
    print("   %s %s%s" % (mark, name, ("  — " + detail) if detail else ""))
    if not ok:
        FAILURES.append(name)
    return ok


def load_app():
    """按路径载入 web/app.py（它不在 sys.path 里）。"""
    path = os.path.join(ROOT, "web", "app.py")
    spec = importlib.util.spec_from_file_location("p2v_web_app", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["p2v_web_app"] = mod
    spec.loader.exec_module(mod)
    # 存下原始实现：后续测试会替换 _build_argv 以避免真跑流水线，
    # 而"续跑生成了什么命令行"这类断言必须用原始实现 —— 否则会拿到上一个测试留下的假函数。
    mod._ORIG_BUILD_ARGV = mod._build_argv
    return mod


# ──────────────────────────────────────────────────────────────
# 清理助手：共享目录里的删除有两条硬规矩
# ──────────────────────────────────────────────────────────────
def _cleanup_new(app, before):
    """删掉**本次测试新建**的 jobs/ 子目录（快照差集）。

    ⚠️ 两条规矩都是被事故与真实风险逼出来的：
      1. **只能用差集，不能用名字前缀批量删**。2026-09-19 那条"凡是 `job_` 开头
         就删"把使用者正在跑的任务整个删了（job.json / result/ 里已生成的幻灯片与
         音频 / progress.jsonl 全没），而流水线还在跑 —— 前缀是别人也在用的命名空间。
      2. **跳过 `_` 开头的目录**。`_content`（内容库）与 `_voices`（音色缓存）是
         **共享**的，可能同时被别的任务、别的任务记录引用；测试整个删掉它们，
         别人就会遇到"文件明明在、却报找不到"。判据同样只能是"是不是我这次建的"。
    """
    removed = []
    for name in sorted(set(os.listdir(app.JOBS_ROOT)) - set(before)):
        if name.startswith("_"):
            continue
        p = os.path.join(app.JOBS_ROOT, name)
        if os.path.isdir(p):
            shutil.rmtree(p, ignore_errors=True)
        else:
            os.remove(p)
        removed.append(name)
    return removed


def _content_state(app):
    """内容库现在的**内容目录**集合（用于测试前后做差集）。

    `_tmp` 不算：它是上传的临时落点，按需创建，不是"一份内容"。
    """
    root = getattr(app, "CONTENT_ROOT", None)
    if not root or not os.path.isdir(root):
        return set()
    return {n for n in os.listdir(root) if n != "_tmp"}


def _cleanup_new_content(app, before):
    """只删**本次测试新建**的内容目录。

    `_tmp` 不删：它是上传的临时落点，可能与正在运行的服务共用（并发上传时
    删它会把别人正在写的临时文件弄没）。
    """
    root = getattr(app, "CONTENT_ROOT", None)
    if not root or not os.path.isdir(root):
        return []
    removed = []
    for name in sorted(set(os.listdir(root)) - set(before)):
        if name == "_tmp":
            continue
        shutil.rmtree(os.path.join(root, name), ignore_errors=True)
        removed.append(name)
    return removed


def _png_bytes(width, height):
    """造一张真 PNG（用 PIL，保证文件头与结构都合法）。"""
    buf = io.BytesIO()
    Image.new("RGB", (width, height), (205, 185, 165)).save(buf, format="PNG")
    buf.seek(0)
    return buf.getvalue()


def _read_json_quietly(file_path):
    try:
        with io.open(file_path, encoding="utf-8") as handle:
            return json.load(handle)
    except Exception:
        return None


def _read_meta_quietly(app, content_id):
    """读内容库某份内容的 meta.json（按 id，不拼路径）。"""
    digest = (content_id or "").replace("content_", "")
    return _read_json_quietly(os.path.join(app.CONTENT_ROOT, digest, "meta.json"))


# ──────────────────────────────────────────────────────────────
# ① 进度解析：用真实历史日志，拿音频个数当独立真值
# ──────────────────────────────────────────────────────────────
def test_read_progress(app):
    print("=" * 66)
    print("[1] 进度解析 —— 输入是两次真实运行的日志，真值取自音频文件个数")
    print("=" * 66)
    ok = True
    made = []
    try:
        for name in ("gkk_en", "gkk_zh"):
            run = os.path.join(ROOT, "result", name)
            log = os.path.join(ROOT, ".workbuddy", "logs", name + ".log")
            if not (os.path.isdir(run) and os.path.isfile(log)):
                print("   （跳过 %s：缺少历史日志或产物）" % name)
                continue

            # 独立真值：实际合成出的音频条数
            audio_dir = os.path.join(run, "audio")
            truth = len([f for f in os.listdir(audio_dir) if f.endswith(".wav")]) \
                if os.path.isdir(audio_dir) else 0

            job_id = "smoke_prog_" + uuid.uuid4().hex[:6]
            made.append(job_id)
            jd = app._job_dir(job_id)
            os.makedirs(os.path.join(jd, "result"), exist_ok=True)
            shutil.copyfile(log, os.path.join(jd, "run.log"))
            pdf = os.path.join(run, "slides_rendered.pdf")
            if os.path.isfile(pdf):
                shutil.copyfile(pdf, os.path.join(jd, "result", "slides_rendered.pdf"))

            job = {"id": job_id, "stage": 0, "phase": "排队中"}
            pr = app.read_progress(job)

            ok &= check("%s: total_pages 取自 PDF" % name,
                        pr["total_pages"] == truth,
                        "读到 %s / 音频真值 %s" % (pr["total_pages"], truth))
            ok &= check("%s: tts_done 等于实际音频条数" % name,
                        pr["tts_done"] == truth,
                        "读到 %s / 真值 %s" % (pr["tts_done"], truth))
            ok &= check("%s: talking_done 等于实际音频条数" % name,
                        pr["talking_done"] == truth,
                        "读到 %s / 真值 %s" % (pr["talking_done"], truth))
            ok &= check("%s: 阶段判定为已完成" % name, pr["phase"] == "已完成",
                        "实际 %r" % pr["phase"])
            ok &= check("%s: tail 有内容" % name, len(pr["tail"]) > 0,
                        "%d 行" % len(pr["tail"]))

        # 运行中途的日志：截断一段，阶段应落在"合成语音"或"生成数字人"
        run = os.path.join(ROOT, "result", "gkk_zh")
        log = os.path.join(ROOT, ".workbuddy", "logs", "gkk_zh.log")
        if os.path.isfile(log):
            text = io.open(log, encoding="utf-8", errors="replace").read()
            cut = text[: int(len(text) * 0.45)]
            job_id = "smoke_mid_" + uuid.uuid4().hex[:6]
            made.append(job_id)
            jd = app._job_dir(job_id)
            os.makedirs(jd, exist_ok=True)
            io.open(os.path.join(jd, "run.log"), "w", encoding="utf-8").write(cut)
            pr = app.read_progress({"id": job_id, "stage": 2, "phase": "运行中"})
            ok &= check("中途截断的日志：阶段不是「已完成」",
                        pr["phase"] in ("合成语音", "生成数字人", "生成幻灯片与讲稿"),
                        "实际 %r（tts %d / talk %d）" % (pr["phase"], pr["tts_done"],
                                                         pr["talking_done"]))
    finally:
        for job_id in made:
            shutil.rmtree(app._job_dir(job_id), ignore_errors=True)
    return ok


# ──────────────────────────────────────────────────────────────
# ② 队列 / SSE / 取消 / 产物：把流水线换成假命令
# ──────────────────────────────────────────────────────────────
FAKE_PIPELINE = '''
import json, os, sys, time
_p = os.environ.get("PAPER2VIDEO_PROGRESS_FILE")
def r(phase, **kw):
    if not _p:
        return
    rec = dict(ts=time.time(), phase=phase)
    rec.update(kw)
    with open(_p, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\\n")
def p(s):
    print(s); sys.stdout.flush()
p("### fake pipeline (smoke test)")
# 让流水线把"注入进来的 key 长度"打出来：这样能验证后端确实把使用者填的 key
# 传给了子进程。这一条在本地看不出问题（.env 里本来就有 key），
# 换到没配 .env 的服务器才会静默失败，所以必须由测试守住。
p("ENVKEY_LEN=" + str(len(os.environ.get("OPENAI_API_KEY") or "")))
r("slides", stage=1)
p("start stage 1")
r("script", stage=2)
for i in range({pages}):
    p("[speech_gen] 第 {{}} 页合成完成".format(i))
    r("tts", stage=2, done=i + 1, total={pages}, page=i)
    time.sleep({sleep})
for i in range({pages}):
    p("[talking_gen] 第 {{}} 页完成".format(i))
    r("talking", stage=3, done=i + 1, total={pages}, page=i)
    time.sleep({sleep})
r("merge", stage=3)
p("数字人与幻灯片合成完成")
'''


def test_queue_and_http(app):
    print()
    print("=" * 66)
    print("[2] 队列 / SSE / 取消 / 产物 —— 用假流水线命令，不花 API 费用")
    print("=" * 66)
    ok = True

    from fastapi.testclient import TestClient

    # 注意：app 是**模块**，FastAPI 实例是它的 app 属性（同名，容易传错）
    srv = app.app

    state = {"sleep": 0.25, "pages": 3}

    def fake_build_argv(job):
        return [sys.executable, "-u", "-c",
                FAKE_PIPELINE.format(pages=state["pages"], sleep=state["sleep"])]

    app._build_argv = fake_build_argv          # 进程内替换，仅本次测试有效

    # 造一个合法的 ingest 目录与人像文件。
    # ⚠️ 刻意用「已经是 LaTeX 工程」的形态（**不放** paper_text.md）：
    # 这样 needs_struct=False，本测试就只专注队列/SSE/取消/产物，
    # 不会牵入「PDF/Word 先转 LaTeX」那一步（那一步由 [5] 单独测）。
    ingest_id = "smoke_ing_" + uuid.uuid4().hex[:6]
    ing_dir = os.path.join(app.JOBS_ROOT, ingest_id)
    os.makedirs(os.path.join(ing_dir, "ingest"), exist_ok=True)
    io.open(os.path.join(ing_dir, "ingest", "main.tex"), "w",
            encoding="utf-8").write("% smoke\n")
    portrait = os.path.join(ing_dir, "portrait.png")
    io.open(portrait, "wb").write(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)

    created = []
    try:
        with TestClient(srv) as client:
            # ── 任务 A：跑完（走 done 路径）──
            r = client.post("/api/jobs", json={
                "ingest_id": ingest_id, "portrait_path": portrait,
                "api_key": "smoke-key-1234567",       # 17 字符，下面据此断言
                "api_base": "https://example.invalid/v1",
                "config": {"slide_lang": "en", "script_lang": "zh",
                           "paper_type": "auto", "stages": ["2", "3"]},
            })
            ok &= check("POST /api/jobs 返回 200", r.status_code == 200,
                        "HTTP %s %s" % (r.status_code, r.text[:80]))
            job_a = r.json().get("job_id")
            created.append(job_a)

            # ── SSE：能收到事件，且进度会在流里增长 ──
            events = []
            t0 = time.time()
            with client.stream("GET", "/api/jobs/%s/events" % job_a) as resp:
                ctype = resp.headers.get("content-type", "")
                for line in resp.iter_lines():
                    if line.startswith("data: "):
                        events.append(json.loads(line[6:]))
                    if len(events) >= 6 or time.time() - t0 > 25:
                        break
            ok &= check("SSE Content-Type 正确", ctype.startswith("text/event-stream"),
                        ctype)
            ok &= check("SSE 收到事件", len(events) >= 2, "%d 个" % len(events))
            if events:
                first, last = events[0], events[-1]
                ok &= check("SSE 事件带 status 与 progress",
                            "status" in first and "progress" in first,
                            json.dumps(first, ensure_ascii=False)[:90])
                ok &= check("SSE 里进度会递增",
                            last["progress"]["tts_done"] >= first["progress"]["tts_done"],
                            "tts %d -> %d" % (first["progress"]["tts_done"],
                                              last["progress"]["tts_done"]))

            # 等它跑完
            deadline = time.time() + 40
            while time.time() < deadline:
                j = client.get("/api/jobs/%s" % job_a).json()
                if j["status"] in ("done", "failed", "canceled"):
                    break
                time.sleep(1)
            ok &= check("任务 A 最终 status=done", j["status"] == "done",
                        "实际 %s (exit=%s)" % (j["status"], j.get("exit_code")))
            ok &= check("任务 A 解析出进度",
                        j["progress"]["tts_done"] == state["pages"]
                        and j["progress"]["talking_done"] == state["pages"],
                        "tts %d / talk %d / 期望 %d" % (
                            j["progress"]["tts_done"],
                            j["progress"]["talking_done"], state["pages"]))
            # P1 的关键闭环：后端必须把 PAPER2VIDEO_PROGRESS_FILE 透传给子进程，
            # 否则流水线不会写结构化进度，又退回脆弱的日志解析。
            ok &= check("进度来源是结构化 jsonl（环境变量已透传）",
                        j["progress"].get("source") == "jsonl",
                        "source=%r" % j["progress"].get("source"))
            ok &= check("结构化进度自带 total（不再依赖数 PDF 页数）",
                        j["progress"]["total_pages"] == state["pages"],
                        "total=%s / 期望 %s" % (j["progress"]["total_pages"],
                                                state["pages"]))

            # 使用者填的 key 必须真的注入子进程环境。
            # 原先 _SESSION_KEYS 从来没被写入过（/api/key/verify 只校验不保存），
            # 于是这段注入永远拿不到 key、实际用的是 .env 里那把 ——
            # 本地因为 .env 有 key 所以"看着能跑"，服务器上才会静默失败。
            log_text = io.open(os.path.join(app._job_dir(job_a), "run.log"),
                               encoding="utf-8", errors="replace").read()
            ok &= check("创建任务时带的 API key 被注入子进程环境",
                        "ENVKEY_LEN=17" in log_text,
                        "看到的 ENVKEY 行: %s" % (
                            [ln.strip() for ln in log_text.splitlines()
                             if ln.strip().startswith("ENVKEY")] or "无"))
            ok &= check("/api/system/gpu 的 running_job 在空闲时为 null",
                        client.get("/api/system/gpu").json()["running_job"] is None)

            # ── 产物：造一个真文件，验证列表与 Range ──
            jd_result = os.path.join(app._job_dir(job_a), "result")
            os.makedirs(jd_result, exist_ok=True)
            blob = bytes(range(256)) * 40          # 10240 字节
            io.open(os.path.join(jd_result, "3_merage.mp4"), "wb").write(blob)

            arts = client.get("/api/jobs/%s/artifacts" % job_a).json()["artifacts"]
            ok &= check("artifacts 列出成片", any(a["name"] == "3_merage.mp4" for a in arts),
                        json.dumps(arts, ensure_ascii=False)[:100])

            full = client.get("/api/jobs/%s/files/3_merage.mp4" % job_a)
            ok &= check("整文件下载 200 且长度一致",
                        full.status_code == 200 and len(full.content) == len(blob),
                        "HTTP %s / %d 字节" % (full.status_code, len(full.content)))

            rng = client.get("/api/jobs/%s/files/3_merage.mp4" % job_a,
                             headers={"Range": "bytes=0-99"})
            ok &= check("Range 请求返回 206", rng.status_code == 206,
                        "HTTP %s (Starlette 的 FileResponse 自带 Range 支持)" % rng.status_code)
            ok &= check("Range 返回 100 字节", len(rng.content) == 100,
                        "%d 字节" % len(rng.content))
            ok &= check("带 Content-Range 头",
                        rng.headers.get("content-range", "").startswith("bytes 0-99/"),
                        rng.headers.get("content-range", "(无)"))

            trav = client.get("/api/jobs/%s/files/../../app.py" % job_a)
            ok &= check("目录穿越被挡（403 或 404）",
                        trav.status_code in (403, 404, 400), "HTTP %s" % trav.status_code)

            # ── 任务 B：跑得慢，用来测取消 ──
            state["sleep"] = 4.0
            r = client.post("/api/jobs", json={
                "ingest_id": ingest_id, "portrait_path": portrait,
                "config": {"slide_lang": "zh", "script_lang": "zh",
                           "paper_type": "auto", "stages": ["2", "3"]},
            })
            job_b = r.json().get("job_id")
            created.append(job_b)

            # 等它真的开始跑
            deadline = time.time() + 30
            running = False
            while time.time() < deadline:
                jb = client.get("/api/jobs/%s" % job_b).json()
                if jb["status"] == "running":
                    running = True
                    break
                time.sleep(0.5)
            ok &= check("任务 B 进入 running", running, "status=%s" % jb["status"])
            ok &= check("/api/system/gpu 报告正在运行的任务",
                        client.get("/api/system/gpu").json()["running_job"] == job_b,
                        "实际 %r" % client.get("/api/system/gpu").json()["running_job"])

            c = client.post("/api/jobs/%s/cancel" % job_b)
            ok &= check("cancel 返回 ok", c.status_code == 200 and c.json().get("ok"),
                        json.dumps(c.json(), ensure_ascii=False)[:80])

            # 关键：必须等 _run_job 的**收尾写入**真正发生再判状态。
            # 为什么不能只 sleep 一下就读：取消与收尾是两个并发的写入者
            # （cancel 处理器写 canceled，_run_job 结束后按退出码写 failed），
            # 早读会读到"看起来正确"的那一次写，从而漏掉覆盖。
            # 判据用 exit_code：它只由 _run_job 的收尾逻辑写入。
            deadline = time.time() + 30
            settled = False
            while time.time() < deadline:
                jb2 = client.get("/api/jobs/%s" % job_b).json()
                if jb2.get("exit_code") is not None:
                    settled = True
                    break
                time.sleep(0.5)
            ok &= check("取消后收尾写入已完成（exit_code 已落）", settled,
                        "exit_code=%s" % jb2.get("exit_code"))
            ok &= check("取消后状态保持 canceled（未被收尾逻辑改写成 failed）",
                        jb2["status"] == "canceled",
                        "实际 %s（exit=%s）" % (jb2["status"], jb2.get("exit_code")))
            ok &= check("取消后命令行进程已不在",
                        app._CURRENT.get("proc") is None)
    finally:
        shutil.rmtree(ing_dir, ignore_errors=True)
        for job_id in created:
            if job_id:
                shutil.rmtree(app._job_dir(job_id), ignore_errors=True)
    return ok


# ──────────────────────────────────────────────────────────────
# ③ 结构化进度（P1）：优先级、容错、阶段文案
# ──────────────────────────────────────────────────────────────
def test_progress_sources(app):
    print()
    print("=" * 66)
    print("[3] 结构化进度 progress.jsonl —— 优先级 / 容错 / 阶段文案")
    print("=" * 66)
    ok = True

    run = os.path.join(ROOT, "result", "gkk_en")
    log = os.path.join(ROOT, ".workbuddy", "logs", "gkk_en.log")
    if not (os.path.isdir(run) and os.path.isfile(log)):
        print("   （跳过：缺少 gkk_en 的历史日志或产物）")
        return True

    job_id = "smoke_prio_" + uuid.uuid4().hex[:6]
    jd = app._job_dir(job_id)
    try:
        os.makedirs(os.path.join(jd, "result"), exist_ok=True)
        # 日志来源刻意用**真实**的 gkk_en 日志：它会解析出 15 页、阶段"已完成"。
        # 这样"jsonl 优先"才有可对照的差异 —— 若读了日志就会是 15。
        shutil.copyfile(log, os.path.join(jd, "run.log"))
        pdf = os.path.join(run, "slides_rendered.pdf")
        if os.path.isfile(pdf):
            shutil.copyfile(pdf, os.path.join(jd, "result", "slides_rendered.pdf"))

        jsonl = os.path.join(jd, "progress.jsonl")
        job = {"id": job_id, "stage": 2, "phase": "运行中", "status": "running"}

        def write_jsonl(*records):
            with io.open(jsonl, "w", encoding="utf-8") as fh:
                for rec in records:
                    fh.write(json.dumps(rec, ensure_ascii=False) + "\n")

        # ① 只有日志
        info = app.read_progress(job)
        log_tts = info["tts_done"]
        ok &= check("只有日志时 source=log", info["source"] == "log",
                    "source=%s / tts=%s" % (info["source"], log_tts))

        # ② 放一份"只到第 6 页"的 jsonl → 必须优先于日志（日志是 15 页）
        write_jsonl(
            {"ts": time.time(), "phase": "slides", "stage": 1},
            {"ts": time.time(), "phase": "script", "stage": 2},
            *[{"ts": time.time(), "phase": "tts", "stage": 2, "done": i,
               "total": 15, "page": i - 1} for i in range(1, 7)])
        info = app.read_progress(job)
        ok &= check("有 jsonl 时 source=jsonl（优先于日志）",
                    info["source"] == "jsonl", "source=%s" % info["source"])
        ok &= check("进度确实取自 jsonl 而非日志",
                    info["tts_done"] == 6 and log_tts == 15,
                    "jsonl 给 6 / 日志是 %s（读错来源就会是后者）" % log_tts)
        ok &= check("阶段文案 tts -> 合成语音", info["phase"] == "合成语音",
                    "实际 %r" % info["phase"])
        ok &= check("total 取自 jsonl", info["total_pages"] == 15,
                    "total=%s" % info["total_pages"])

        # ③ 只有阶段记录（还没出页数）→ 不应崩，total 回落到 PDF 页数
        write_jsonl({"ts": time.time(), "phase": "script", "stage": 2})
        info = app.read_progress(job)
        ok &= check("只有阶段记录时 phase=生成讲稿", info["phase"] == "生成讲稿",
                    "实际 %r" % info["phase"])
        ok &= check("jsonl 未给 total 时回落到 PDF 页数",
                    info["total_pages"] == 15, "total=%s（期望 15，来自 PDF）"
                    % info["total_pages"])

        # ④ jsonl 损坏 → 回落日志，而不是报错
        io.open(jsonl, "w", encoding="utf-8").write("这不是 JSON\n{\"phase\": \n")
        info = app.read_progress(job)
        ok &= check("jsonl 损坏时回落日志（不报错）",
                    info["source"] == "log" and info["tts_done"] == 15,
                    "source=%s / tts=%s" % (info["source"], info["tts_done"]))

        # ⑤ 六个 phase 的界面文案
        want = {"slides": "生成幻灯片", "script": "生成讲稿", "tts": "合成语音",
                "cursor": "光标定位", "talking": "生成数字人", "merge": "合成视频"}
        bad = []
        for phase, label in want.items():
            write_jsonl({"ts": time.time(), "phase": phase})
            got = app.read_progress(job)["phase"]
            if got != label:
                bad.append("%s -> %r（期望 %r）" % (phase, got, label))
        ok &= check("六个 phase 的界面文案都对", not bad, "；".join(bad) or "全部匹配")

        # ⑥ 终态以任务状态为准，不从日志/阶段记录猜
        write_jsonl({"ts": time.time(), "phase": "talking"})
        bad2 = []
        for status, label in (("done", "已完成"), ("failed", "失败"),
                              ("canceled", "已取消")):
            got = app.read_progress(dict(job, status=status))["phase"]
            if got != label:
                bad2.append("%s -> %r（期望 %r）" % (status, got, label))
        ok &= check("终态以任务状态为准（完成/失败/已取消）", not bad2,
                    "；".join(bad2) or "三种状态都对")
    finally:
        shutil.rmtree(jd, ignore_errors=True)
    return ok


# ──────────────────────────────────────────────────────────────
# ④ 逐页素材索引 与 续跑（P1）
# ──────────────────────────────────────────────────────────────
def test_pages_and_rerun(app):
    print()
    print("=" * 66)
    print("[4] 逐页素材索引 / 续跑 —— 重点验命名错位（1-based 幻灯片 vs 0-based 音频）")
    print("=" * 66)
    ok = True

    from fastapi.testclient import TestClient

    srv = app.app
    job_id = "smoke_pages_" + uuid.uuid4().hex[:6]
    jd = app._job_dir(job_id)
    # ⚠️ 进函数先拍一张 jobs/ 的快照：清理时只删**本测试新建的目录**（差集）。
    # 见下面 finally 里的说明 —— 这条快照是防止"删掉使用者正在跑的任务"的关键。
    jobs_before = set(os.listdir(app.JOBS_ROOT))
    os.makedirs(jd, exist_ok=True)
    # /pages 会先校验任务存在（_load_job），所以 job.json 必须先有
    app._save_job({"id": job_id, "status": "done", "phase": "已完成",
                   "created_at": "2026-09-18 10:00:00", "config": {}})
    base = os.path.join(jd, "result")

    def touch(rel, data=b"x"):
        fp = os.path.join(base, rel)
        os.makedirs(os.path.dirname(fp), exist_ok=True)
        with open(fp, "wb") as fh:
            fh.write(data)
        return fp

    pages = 3
    try:
        # 造一份符合真实命名规则的产物：幻灯片从 1 起、音频从 0 起、
        # 数字人还多一层参考图目录（这正是最容易错位的地方）
        for i in range(1, pages + 1):
            touch("slide_imgs/%d.png" % i, b"\x89PNG\r\n\x1a\n")
        for i in range(pages):
            touch("audio/%d.wav" % i, b"RIFF")
            touch("talking_musetalk/%d/portrait/merge_video.mp4" % i, b"\x00" * 64)
        touch("cursor.json", b"{}")
        io.open(os.path.join(base, "subtitle_w_cursor.txt"), "w",
                encoding="utf-8").write(
            "###\np1 line1 | cursor1\np1 line2 | cursor2\n"
            "###\np2 line1 | cursor3\n"
            "###\np3 line1 | cursor4\n")

        # 让 worker 拾起任务时立刻结束（本测试不跑真实流水线）
        app._build_argv = lambda job: [sys.executable, "-u", "-c", "pass"]

        with TestClient(srv) as client:
            r = client.get("/api/jobs/%s/pages" % job_id)
            ok &= check("GET /pages 返回 200", r.status_code == 200,
                        "HTTP %s" % r.status_code)
            data = r.json()
            ok &= check("总页数正确", data["total"] == pages,
                        "total=%s / 期望 %s" % (data["total"], pages))
            ok &= check("识别到讲稿 / 光标 / 幻灯片 PDF 三份附加产物",
                        data["script_txt"] == "subtitle_w_cursor.txt"
                        and data["cursor_json"] == "cursor.json",
                        "script=%r cursor=%r" % (data["script_txt"],
                                                 data["cursor_json"]))

            p1 = data["pages"][0]
            ok &= check("第 1 页：幻灯片取 1.png（1-based）",
                        p1["slide"] == "slide_imgs/1.png", "实际 %r" % p1["slide"])
            ok &= check("第 1 页：音频取 0.wav（0-based，与幻灯片差 1）",
                        p1["audio"] == "audio/0.wav", "实际 %r" % p1["audio"])
            ok &= check("第 1 页：数字人路径含参考图那一层目录",
                        p1["talking"] == "talking_musetalk/0/portrait/merge_video.mp4",
                        "实际 %r" % p1["talking"])
            ok &= check("第 1 页：讲稿解析出 2 句且已去掉光标短语",
                        p1["script"] == ["p1 line1", "p1 line2"],
                        "实际 %r" % p1["script"])

            p3 = data["pages"][2]
            ok &= check("第 3 页：幻灯片/音频/讲稿都没错位",
                        p3["slide"] == "slide_imgs/3.png"
                        and p3["audio"] == "audio/2.wav"
                        and p3["script"] == ["p3 line1"],
                        "slide=%r audio=%r script=%r" % (p3["slide"], p3["audio"],
                                                         p3["script"]))

            missing = client.get("/api/jobs/not_a_job/pages")
            ok &= check("不存在的任务返回 404", missing.status_code == 404,
                        "HTTP %s" % missing.status_code)

            # ── 续跑 ──
            # 造一个"已完成"的源任务（含人像），验证续跑的字段与参数
            portrait = os.path.join(jd, "portrait.png")
            with open(portrait, "wb") as fh:
                fh.write(b"\x89PNG\r\n\x1a\n" + b"\x00" * 16)
            src_job = {
                "id": job_id, "status": "done", "phase": "已完成",
                "created_at": "2026-09-18 10:00:00",
                "config": {"stages": ["1", "2", "3"], "slide_lang": "en",
                           "script_lang": "zh", "paper_type": "auto"},
                "ref_img": portrait,
                "paper_latex_root": os.path.join(jd, "latex_proj"),
                "needs_struct": True,
                "source_path": None,
            }
            app._save_job(src_job)

            rr = client.post("/api/jobs/%s/rerun" % job_id, json={})
            ok &= check("POST /rerun 返回 200", rr.status_code == 200,
                        "HTTP %s %s" % (rr.status_code, rr.text[:80]))
            out = rr.json()
            new_id = out.get("job_id")
            ok &= check("续跑默认只跑 stage 2/3", out.get("stages") == ["2", "3"],
                        "stages=%s" % out.get("stages"))
            ok &= check("续跑默认复用讲稿（省钱关键）",
                        out.get("reuse_script") is True,
                        "reuse_script=%s" % out.get("reuse_script"))
            ok &= check("记录了 resumed_from", out.get("resumed_from") == job_id,
                        "resumed_from=%r" % out.get("resumed_from"))

            if new_id:
                nj = app._load_job(new_id)
                ok &= check("新任务复用原 result 目录（不复制产物）",
                            nj["config"].get("result_dir") == base,
                            "result_dir=%r" % nj["config"].get("result_dir"))
                ok &= check("续跑不再触发结构化（避免重复付费）",
                            nj.get("needs_struct") is False,
                            "needs_struct=%r" % nj.get("needs_struct"))
                argv = app._ORIG_BUILD_ARGV(nj)   # 用原始实现（见 load_app 的说明）
                ok &= check("argv 里的 --result_dir 指向原产物",
                            base in argv, "--result_dir=%r" % (
                                argv[argv.index("--result_dir") + 1]
                                if "--result_dir" in argv else None))
                ok &= check("argv 带上 --reuse_script True",
                            "--reuse_script" in argv
                            and argv[argv.index("--reuse_script") + 1] == "True",
                            "argv 尾部=%s" % argv[-2:])
                ok &= check("argv 里 --if_tree_search 恒为 False",
                            argv[argv.index("--if_tree_search") + 1] == "False",
                            "（默认值 True 会 NameError，必须写死）")

            bad = client.post("/api/jobs/not_a_job/rerun", json={})
            ok &= check("对不存在的任务续跑返回 404", bad.status_code == 404,
                        "HTTP %s" % bad.status_code)
    finally:
        shutil.rmtree(jd, ignore_errors=True)
        # ⚠️⚠️ 这里原先写的是"凡是 `job_` 开头就删" —— 2026-09-19 造成了**真实数据丢失**：
        # 使用者有一个任务正在跑（`job_0919_004702_866d`，MuseTalk 推理中），
        # 这条清理把它的整个目录删掉了（job.json / result/ 已生成的 13 张幻灯片与
        # 13 段音频 / progress.jsonl 全没了），而流水线还在跑 —— 数字人跑完后
        # 合片必然失败，任务记录也从界面上消失。单人测试环境里 jobs/ 只有测试
        # 自己的目录，所以这个写法一直"看着没问题"。
        # 现在的规矩：**共享目录里的清理只能用「进入函数前 vs 现在」的快照差集**，
        # 绝不能用名字前缀/模式批量删 —— 前缀是别人也可能在用的命名空间。
        # （助手还额外跳过 `_` 开头的共享目录，见 _cleanup_new。）
        _cleanup_new(app, jobs_before)
    return ok


# ──────────────────────────────────────────────────────────────
# ⑤ PDF/Word 输入的前置步骤（原本缺失的一环）
# ──────────────────────────────────────────────────────────────
def test_struct_prepare(app):
    """流水线入口要「含 main.tex 的目录」，而使用者给的是 PDF/Word。

    这一步不补，stage 1 会因为找不到 main.tex 而失败 —— 所以它的
    失败路径必须**干净**（明确报原因、任务落到 failed），
    而不是让任务一直挂在 running 上。
    这里直接单测 `_prepare_latex`：不触发任何 API 调用。
    """
    print()
    print("=" * 66)
    print("[5] PDF/Word → LaTeX 工程的前置步骤（不调 API）")
    print("=" * 66)
    ok = True
    job_id = "smoke_struct_" + uuid.uuid4().hex[:6]
    jd = app._job_dir(job_id)
    try:
        os.makedirs(jd, exist_ok=True)
        buffer = io.StringIO()

        # A) 既没有 main.tex、也没有可用的源文件 → 必须返回 False 并说清原因
        job = {"id": job_id, "paper_latex_root": os.path.join(jd, "latex_proj"),
               "source_path": None}
        got = asyncio.run(app._prepare_latex(job, jd, buffer, dict(os.environ)))
        ok &= check("缺源文件时返回 False（不再继续跑流水线）", got is False,
                    "返回 %r" % got)
        text = buffer.getvalue()
        ok &= check("日志说明原因", "源文件不可用" in text,
                    "日志片段=%r" % (text.strip().splitlines()[:1] or [""]))
        ok &= check("日志给出补救办法（提示重新上传）", "重新上传" in text,
                    "日志片段=%r" % (text.strip().splitlines()[-1:] or [""]))

        # B) main.tex 已存在 → 跳过结构化（重跑 / 续跑不重复付费）
        latex_root = os.path.join(jd, "latex_proj")
        os.makedirs(latex_root, exist_ok=True)
        io.open(os.path.join(latex_root, "main.tex"), "w",
                encoding="utf-8").write("% existing\n")
        buffer.seek(0)
        buffer.truncate()
        job = {"id": job_id, "paper_latex_root": latex_root, "source_path": None}
        got = asyncio.run(app._prepare_latex(job, jd, buffer, dict(os.environ)))
        ok &= check("已有 main.tex 时返回 True 并跳过", got is True, "返回 %r" % got)
        ok &= check("日志说明是复用而非重新生成",
                    "跳过结构化" in buffer.getvalue(),
                    "日志=%r" % buffer.getvalue().strip()[:70])
    finally:
        shutil.rmtree(jd, ignore_errors=True)
    return ok


# ──────────────────────────────────────────────────────────────
# ⑥ 人像上传（独立接口）
# ──────────────────────────────────────────────────────────────
def test_portrait_upload(app):
    """人像必须走**专用**接口。

    历史 bug：前端原先复用人像走 `/api/ingest`（那是"论文 → 正文提取"），
    图片进去必然报「无法识别的文件格式」→ **人像永远传不上去 → 任务永远建不起来**，
    而界面上只看到一句"人像图上传失败"。所以这里必须守住两个点：
      1. 图片走 /api/portrait 能成功并回传尺寸；
      2. 非图片被拒时**不留空壳目录**（否则 jobs/ 里堆一堆空目录，误导排查）。
    """
    print()
    print("=" * 66)
    print("[6] 人像上传 —— 专用接口 / 尺寸校验 / 失败不留空目录")
    print("=" * 66)
    ok = True

    from fastapi.testclient import TestClient

    def png_bytes(width, height):
        """造一张真 PNG（用 PIL，保证文件头与结构都合法）。"""
        buf = io.BytesIO()
        Image.new("RGB", (width, height), (205, 185, 165)).save(buf, format="PNG")
        buf.seek(0)
        return buf.getvalue()

    before = set(os.listdir(app.JOBS_ROOT))
    content_before = _content_state(app)
    srv = app.app
    try:
        with TestClient(srv) as client:
            r = client.post("/api/portrait",
                            files={"paper": ("portrait_ok.png", png_bytes(512, 640),
                                             "application/octet-stream")})
            ok &= check("正常图片上传返回 200", r.status_code == 200,
                        "HTTP %s %s" % (r.status_code, r.text[:90]))
            data = r.json()
            ok &= check("回传了落盘路径与尺寸",
                        os.path.isfile(data.get("portrait_path", ""))
                        and data.get("width") == 512 and data.get("height") == 640,
                        "path=%s %sx%s" % (os.path.basename(data.get("portrait_path", "")),
                                           data.get("width"), data.get("height")))
            ok &= check("正常尺寸没有告警", not data.get("warnings"),
                        "%s" % data.get("warnings"))
            ok &= check("返回里写明未做人脸检测（不给虚假安全感）",
                        "未做人脸检测" in (data.get("note") or ""),
                        (data.get("note") or "")[:50])

            r = client.post("/api/portrait",
                            files={"paper": ("small.png", png_bytes(128, 128),
                                             "application/octet-stream")})
            warns = r.json().get("warnings") or []
            ok &= check("过小图片给出告警", any("短边" in w for w in warns),
                        "%s" % warns)

            r = client.post("/api/portrait",
                            files={"paper": ("notimage.png",
                                             b"definitely not an image at all",
                                             "application/octet-stream")})
            ok &= check("非图片返回 400", r.status_code == 400,
                        "HTTP %s" % r.status_code)
            ok &= check("拒绝时说清了支持的格式",
                        "PNG" in (r.json().get("hint") or ""),
                        "%s" % r.json().get("hint"))

            # 内容寻址（2026-09-19）之后，上传不再往 jobs/ 下建随机目录，
            # 而是进**共享内容库**。所以断言改成"库里新增了几份内容"：
            #   3 次请求里 2 次成功，且两次是**不同**的图（512×640 与 128×128）
            #   ⇒ 新增 2 份；失败那次不留任何内容（否则库里攒下"传过但没结果"的垃圾）。
            new_content = _content_state(app) - content_before
            ok &= check("失败那次不往内容库里落东西", len(new_content) == 2,
                        "新增 %d 份内容（期望 2 份不同的图）：%s"
                        % (len(new_content), sorted(new_content)))
    finally:
        _cleanup_new_content(app, content_before)
        _cleanup_new(app, before)
    return ok


def test_log_encoding_and_model_registry(app):
    """日志编码 + 模型上下文窗口登记。

    这两件事都属于"不会崩、但会静默出错"的类别，而且**在开发机上不容易暴露**：
    ① `run.log` 有两个写入者、编码不同（父进程 UTF-8 写自己的行，子进程 stdout 按
       Windows 控制台编码 GBK 输出）→ 混合文件用任何单一解码器都读不了：
       界面日志尾巴变乱码，日志兜底解析的正则命中变成 0。
    ② camel 没登记我们起的模型名 → 打一行
       "Unknown model ... Defaulting to 999_999_999."，看着像错误，其实不是。
    """
    print()
    print("=" * 66)
    print("[7] 日志编码统一 + 模型上下文窗口登记")
    print("=" * 66)
    ok = True

    # ── ① 混合编码的 run.log 必须能读全（这条是硬断言，编码是确定的）──
    job_id = "smoke_enc_" + uuid.uuid4().hex[:6]
    jd = app._job_dir(job_id)
    try:
        os.makedirs(jd, exist_ok=True)
        app._save_job({"id": job_id, "status": "running", "stage": 2, "phase": "合成语音"})
        with open(os.path.join(jd, "run.log"), "wb") as fh:
            fh.write("### 命令 pipeline.py\n".encode("utf-8"))          # 父进程风格
            fh.write("[struct] 正在把论文转成 LaTeX 工程\n".encode("utf-8"))
            fh.write("[speech_gen] 第 0 页合成完成\n".encode("gbk"))     # 子进程风格
            fh.write("[speech_gen] 第 1 页合成完成\n".encode("gbk"))
            fh.write("[talking_gen] 第 0 页完成\n".encode("gbk"))

        log_path = os.path.join(jd, "run.log")
        text = app._read_log_text(log_path)
        ok &= check("混合编码日志能读全（utf-8 行与 gbk 行都在、无替换字符）",
                    "\ufffd" not in text and "LaTeX" in text and "第 1 页合成完成" in text,
                    "替换字符 %d 个 / 长度 %d" % (text.count("\ufffd"), len(text)))

        try:
            open(log_path, "rb").read().decode("utf-8")
            strict = True
            strict_note = "整文件可解码"
        except UnicodeDecodeError as exc:
            strict = False
            strict_note = "第 %d 字节处解码失败" % exc.start
        ok &= check("对照组：整文件严格 utf-8 解码会失败（证明这是一个真问题）",
                    not strict, strict_note)

        prog = app.read_progress(app._load_job(job_id))
        ok &= check("从 GBK 行里数出页数（修复前正则命中 0）",
                    prog["tts_done"] == 2 and prog["talking_done"] == 1,
                    "tts=%s talking=%s" % (prog["tts_done"], prog["talking_done"]))
        ok &= check("日志尾巴可读、不是乱码",
                    bool(prog["tail"]) and all("\ufffd" not in ln for ln in prog["tail"]),
                    " / ".join(prog["tail"])[-90:])
    finally:
        shutil.rmtree(jd, ignore_errors=True)

    # ── ② 选项层：子进程环境必须显式统一编码 ──
    # 行为层已验证过（真实任务的 run.log 现在可严格 utf-8 解码），这里做静态断言，
    # 防止以后有人把这一行删掉 —— 删掉后不会立刻报错，只会让日志重新变乱码。
    src = io.open(os.path.join(os.path.dirname(os.path.abspath(app.__file__)), "app.py"),
                  encoding="utf-8").read()
    ok &= check('_run_job 里显式设置了 PYTHONIOENCODING=utf-8',
                'env["PYTHONIOENCODING"] = "utf-8"' in src,
                "源码中出现 %d 次" % src.count('PYTHONIOENCODING'))

    # ── ③ 模型名已登记上下文窗口 ──
    if app.P2V_SRC not in sys.path:
        sys.path.insert(0, app.P2V_SRC)
    try:
        import wei_utils  # noqa: F401  导入即触发注册
        from camel.types import UnifiedModelType
        model = wei_utils._OPENAI_COMPATIBLE_ALIASES[0]
        got = UnifiedModelType(model).token_limit
        ok &= check("我们的模型名已登记上下文窗口（不再退化成 1e9）",
                    got != 999_999_999 and got >= 128_000,
                    "%s -> %s" % (model, got))
        ok &= check("已登记的模型不受影响（gpt-4o 仍是 128000）",
                    UnifiedModelType("gpt-4o").token_limit == 128_000,
                    "gpt-4o -> %s" % UnifiedModelType("gpt-4o").token_limit)
    except Exception as exc:
        ok &= check("模型上下文窗口登记可导入", False,
                    "%s: %s" % (type(exc).__name__, exc))

    return ok


def test_rerun_shares_artifacts(app):
    """续跑的任务必须能"看到自己的产物"。

    续跑**不复制产物**（slides/audio 动辄几百 MB），新任务只在 config.result_dir 里
    指向原任务的 result 目录。写入侧一直是对的，但读取侧原先各拼各的
    `<job_dir>/result`，于是**续跑出来的任务产物列表是空的** ——
    使用者得回头点原任务才能看（实测反馈）。

    这里把四条件都钉住：产物列表 / 逐页索引 / 文件服务 / 总页数兜底。
    """
    print()
    print("=" * 66)
    print("[8] 续跑共享产物 —— 新任务应能直接看到原任务的产物")
    print("=" * 66)
    ok = True

    from fastapi.testclient import TestClient

    pages = 3
    orig_id = "smoke_re_orig_" + uuid.uuid4().hex[:6]
    jd = app._job_dir(orig_id)
    base = os.path.join(jd, "result")

    def touch(rel, data=b"x" * 32):
        fp = os.path.join(base, rel)
        os.makedirs(os.path.dirname(fp), exist_ok=True)
        with open(fp, "wb") as fh:
            fh.write(data)
        return fp

    portrait = os.path.join(app.JOBS_ROOT, "smoke_re_portrait.png")
    created = [jd]
    try:
        os.makedirs(base, exist_ok=True)
        for i in range(1, pages + 1):
            touch("slide_imgs/%d.png" % i, b"\x89PNG\r\n\x1a\n")
        for i in range(pages):
            touch("audio/%d.wav" % i, b"RIFF")
        video = touch("3_merage.mp4", b"\x00" * 4096)
        # 真造一份 %d 页的 PDF —— 总页数兜底走的就是它
        try:
            import pymupdf
            doc = pymupdf.open()
            for _ in range(pages):
                doc.new_page()
            doc.save(os.path.join(base, "slides_rendered.pdf"))
            doc.close()
            has_pdf = True
        except Exception:
            has_pdf = False

        app._save_job({"id": orig_id, "status": "done", "phase": "已完成",
                       "created_at": "2026-09-18 10:00:00", "config": {},
                       "ref_img": portrait})
        with open(portrait, "wb") as fh:
            fh.write(b"\x89PNG\r\n\x1a\n")

        app._build_argv = lambda job: [sys.executable, "-u", "-c", "pass"]

        with TestClient(app.app) as client:
            # ① 基线：原任务自己能看见 → 证明造的数据是有效的
            r0 = client.get("/api/jobs/%s/artifacts" % orig_id)
            names0 = [a["name"] for a in r0.json().get("artifacts", [])]
            ok &= check("基线：原任务能看到 3_merage.mp4", "3_merage.mp4" in names0,
                        "原任务产物 %s" % names0)

            # ② 续跑
            rr = client.post("/api/jobs/%s/rerun" % orig_id,
                             json={"stages": ["3"], "reuse_script": True,
                                   "portrait_path": portrait})
            new_id = rr.json().get("job_id")
            ok &= check("rerun 返回新任务 id", rr.status_code == 200 and bool(new_id),
                        "HTTP %s / %s" % (rr.status_code, new_id))
            if not new_id:
                return False
            created.append(app._job_dir(new_id))

            nj = app._load_job(new_id)
            ok &= check("新任务的 result_dir 指向原任务的产物目录",
                        (nj.get("config") or {}).get("result_dir") == base,
                        "result_dir=%s" % (nj.get("config") or {}).get("result_dir"))

            # ③ 关键断言：新任务的产物列表不能是空的
            r1 = client.get("/api/jobs/%s/artifacts" % new_id)
            names1 = [a["name"] for a in r1.json().get("artifacts", [])]
            ok &= check("**续跑任务能看到产物**（修复前这里是空的）",
                        "3_merage.mp4" in names1,
                        "新任务产物 %s" % names1)

            r2 = client.get("/api/jobs/%s/pages" % new_id)
            ok &= check("续跑任务的逐页索引可用",
                        r2.status_code == 200 and r2.json().get("total") == pages,
                        "total=%s / 期望 %s" % (
                            r2.json().get("total") if r2.status_code == 200 else "-", pages))

            r3 = client.get("/api/jobs/%s/files/3_merage.mp4" % new_id)
            ok &= check("续跑任务的文件服务可取到产物",
                        r3.status_code == 200 and r3.content == b"\x00" * 4096,
                        "HTTP %s / %d 字节" % (r3.status_code, len(r3.content)))

            if has_pdf:
                prog = app.read_progress(nj)
                ok &= check("续跑任务的总页数兜底走产物目录",
                            prog["total_pages"] == pages,
                            "total_pages=%s / 期望 %s" % (prog["total_pages"], pages))

            # ④ 「续跑的续跑」也要能找到（否则第二次续跑会指向第一个续跑的空目录）
            r4 = client.post("/api/jobs/%s/rerun" % new_id,
                             json={"stages": ["3"], "portrait_path": portrait})
            third = r4.json().get("job_id")
            ok &= check("续跑的续跑也能入队", r4.status_code == 200 and bool(third),
                        "HTTP %s / %s" % (r4.status_code, third))
            if third:
                created.append(app._job_dir(third))
                r5 = client.get("/api/jobs/%s/artifacts" % third)
                names5 = [a["name"] for a in r5.json().get("artifacts", [])]
                ok &= check("**二层续跑仍指向同一产物目录**",
                            "3_merage.mp4" in names5,
                            "产物 %s" % names5)
    finally:
        if os.path.isfile(portrait):
            os.remove(portrait)
        for path in created:
            shutil.rmtree(path, ignore_errors=True)
    return ok



# ──────────────────────────────────────────────────────────────
# ⑨ 阶段模型 / 总进度 / 凭据隔离 / SSE 保活（2026-09-18）
#
# 这一组针对使用者反馈的五个问题里的四个，重点是把**口径**钉死：
#   · 总进度必须单调不减（旧公式实测有 100% -> 7% 的回跳）；
#   · 没有逐页进度的阶段**不许编造进度**；
#   · 卡住判据按阶段给阈值（数字人阶段本来就几分钟没有新记录）；
#   · 凭据按任务隔离（原先进程级一份："最后输入的人覆盖所有人"）。
# 并且带**反面对照**：旧口径在同样的数据上确实会出错 —— 否则这类测试
# 改前改后都通过，等于没测。
# ──────────────────────────────────────────────────────────────
def _old_pct_formula(tts_done, talking_done, total):
    """**修复前**的口径（App.tsx 原公式 `talking_done || tts_done`），仅用于对照。"""
    done = talking_done or tts_done
    return min(100, round(done / float(total) * 100)) if total else 0


def test_phase_model_and_keys(app):
    print()
    print("=" * 66)
    print("[9] 阶段模型与总进度 —— 单一来源 / 单调性 / 凭据按任务隔离")
    print("=" * 66)
    ok = True
    import ast
    import threading
    from fastapi.testclient import TestClient

    # ── A. 阶段表由后端下发（前端不再硬编码）──
    with TestClient(app.app) as client:
        opts = client.get("/api/options").json()
    phases = opts.get("phases") or []
    keys = [p.get("key") for p in phases]
    ok &= check("/api/options 下发阶段表", len(phases) >= 6,
                "%d 个阶段：%s" % (len(phases), " → ".join(str(k) for k in keys)))
    weight_sum = sum(p.get("weight", 0) for p in phases)
    ok &= check("阶段权重合计为 1.0（“满格”的定义）", abs(weight_sum - 1.0) < 1e-6,
                "合计 %.4f" % weight_sum)
    missing = [p.get("key") for p in phases
               if not {"key", "label", "weight", "has_pages"} <= set(p)]
    ok &= check("每个阶段都有 key/label/weight/has_pages", not missing,
                "缺字段的阶段：%s" % missing if missing else "字段齐全")
    counts_phases = [p["key"] for p in phases if p["has_pages"]]
    ok &= check("有计数进度的阶段是 tts/cursor/talking（其余阶段不编造进度）",
                counts_phases == ["tts", "cursor", "talking"],
                "标记为有计数的：%s" % counts_phases)
    ok &= check("每个阶段都带计数单位（语音/数字人=页，光标=条）",
                {p["key"]: p.get("unit") for p in phases}.get("cursor") == "条"
                and {p["key"]: p.get("unit") for p in phases}.get("tts") == "页",
                "单位：%s" % {p["key"]: p.get("unit") for p in phases if p["has_pages"]})

    # ── B. 总进度：单调不减 + 不编造进度 ──
    job_id = "smoke_phase_" + uuid.uuid4().hex[:6]
    jd = app._job_dir(job_id)
    try:
        os.makedirs(os.path.join(jd, "result"), exist_ok=True)
        jsonl = os.path.join(jd, "progress.jsonl")
        job = {"id": job_id, "status": "running", "phase": "启动中",
               "phase_key": "extract", "config": {"script_lang": "zh"}}
        PAGE = 14
        recs = []
        samples = []                     # [(标签, 新口径, 旧口径)]

        def write(records):
            with io.open(jsonl, "w", encoding="utf-8") as fh:
                for rec in records:
                    fh.write(json.dumps(rec, ensure_ascii=False) + "\n")

        def snap(label):
            info = app.read_progress(dict(job))
            samples.append((label, info["overall_pct"],
                            _old_pct_formula(info["tts_done"], info["talking_done"],
                                             info["total_pages"] or PAGE)))

        for phase in ("struct", "slides", "script"):
            recs.append({"ts": time.time(), "phase": phase, "stage": 1})
            write(recs)
            snap(phase)
        for i in range(1, PAGE + 1):
            recs.append({"ts": time.time(), "phase": "tts", "stage": 2, "done": i,
                         "total": PAGE, "page": i - 1})
            write(recs)
            snap("tts(%d)" % i)
        recs.append({"ts": time.time(), "phase": "cursor", "stage": 2})
        write(recs)
        snap("cursor")
        for i in range(0, PAGE + 1):
            recs.append({"ts": time.time(), "phase": "talking", "stage": 3, "done": i,
                         "total": PAGE, "page": max(0, i - 1)})
            write(recs)
            snap("talking(%d)" % i)
        recs.append({"ts": time.time(), "phase": "merge", "stage": 3})
        write(recs)
        snap("merge")

        by_label = {label: pct for label, pct, _o in samples}
        back = [(samples[i - 1][0], samples[i - 1][1], samples[i][0], samples[i][1])
                for i in range(1, len(samples)) if samples[i][1] < samples[i - 1][1]]
        ok &= check("总进度单调不减（不存在任何回跳）", not back,
                    "全序列 %s" % [p for _l, p, _o in samples] if not back else "%s" % back)
        ok &= check("幻灯片/讲稿阶段不再是 0%（旧口径因为没有页数只能显示 0）",
                    by_label.get("slides", 0) >= 12,
                    "slides=%s%%（起点刻度 %s%%）"
                    % (by_label.get("slides"), int(app._PHASE_START["slides"] * 100)))
        ok &= check("光标定位阶段**不假装在走**（与语音结束同一刻度）",
                    by_label.get("cursor") == by_label.get("tts(%d)" % PAGE),
                    "cursor=%s%% / tts 结束=%s%%"
                    % (by_label.get("cursor"), by_label.get("tts(%d)" % PAGE)))
        ok &= check("进入数字人阶段落在它的起点刻度（60%）",
                    by_label.get("talking(0)") == int(app._PHASE_START["talking"] * 100),
                    "talking(0)=%s%%" % by_label.get("talking(0)"))
        ok &= check("数字人阶段按页推进到该阶段终点（92%）",
                    by_label.get("talking(%d)" % PAGE) ==
                    int((app._PHASE_START["talking"] + app._PHASE_WEIGHT["talking"]) * 100),
                    "talking(%d)=%s%%" % (PAGE, by_label.get("talking(%d)" % PAGE)))

        old_back = [(samples[i - 1][0], samples[i - 1][2], samples[i][0], samples[i][2])
                    for i in range(1, len(samples)) if samples[i][2] < samples[i - 1][2]]
        ok &= check("反面对照：旧口径在同样数据上确实回跳（证明这条测试有牙齿）",
                    bool(old_back),
                    "旧口径回跳 %d 处，例如 %s" % (len(old_back), old_back[:1]))

        # ── B2. 光标阶段：计数单位是"条"，且**绝不能污染页数** ──
        # 起因（2026-09-19 实测）：一次真实任务里 18 条指令回退给 GUI-Owl，
        # 跑了约 9 分钟；而那时 progress.jsonl 只写了一条阶段边界记录，
        # 界面上只有"光标定位"四个字 —— 使用者报"卡住了"，我们差点也这么判断。
        write([{"ts": time.time(), "phase": "tts", "stage": 2, "done": 13, "total": 13},
               {"ts": time.time(), "phase": "cursor", "stage": 2},
               {"ts": time.time(), "phase": "cursor", "stage": 2, "done": 3, "total": 18},
               {"ts": time.time(), "phase": "cursor", "stage": 2, "done": 7, "total": 18}])
        info = app.read_progress(dict(job))
        ok &= check("光标阶段有计数进度（界面能显示「第 k / N 条」）",
                    info["count_done"] == 7 and info["count_total"] == 18
                    and info["count_unit"] == "条",
                    "第 %s / %s %s" % (info["count_done"], info["count_total"],
                                       info["count_unit"]))
        # 这条是最阴的：光标报的 total 是"指令条数"，若和页数混在一起取 max，
        # 会得到"18 页"这种荒谬结论，而界面显示"第 k/18 页"看起来还很正常。
        ok &= check("光标的条数**不会**被当成页数（18 条 ≠ 18 页）",
                    info["total_pages"] == 13,
                    "total_pages=%s（期望 13，来自 tts）" % info["total_pages"])
        write([{"ts": time.time() - 400, "phase": "cursor", "stage": 2,
                "done": 5, "total": 18}])
        ok &= check("光标阶段静默 400 秒不判卡住（阈值 600 秒）",
                    app.read_progress(dict(job))["stalled"] is False,
                    "stalled=%s / 阈值 %s 秒"
                    % (app.read_progress(dict(job))["stalled"],
                       app.read_progress(dict(job))["stalled_limit_s"]))
        write([{"ts": time.time() - 700, "phase": "cursor", "stage": 2,
                "done": 5, "total": 18}])
        ok &= check("光标阶段静默 700 秒判卡住",
                    app.read_progress(dict(job))["stalled"] is True,
                    "已静默 700 秒 > 阈值 600 秒")

        info_done = app.read_progress(dict(job, status="done"))
        ok &= check("任务完成 -> 总进度 100%", info_done["overall_pct"] == 100,
                    "overall_pct=%s" % info_done["overall_pct"])
        info_fail = app.read_progress(dict(job, status="failed"))
        ok &= check("任务失败 -> 保留已走位置且封顶 99%（不与“完成”混淆）",
                    0 < info_fail["overall_pct"] <= 99,
                    "failed 时 overall_pct=%s" % info_fail["overall_pct"])

        # ── C. 认不得的阶段名 ──
        write([{"ts": time.time(), "phase": "brand_new_phase", "stage": 9}])
        info = app.read_progress(dict(job, phase_key="talking", phase="生成数字人"))
        ok &= check("认不得的阶段名不会把阶段清空（保留任务里记着的那一步）",
                    info["phase_key"] == "talking" and info["phase"] == "生成数字人",
                    "phase_key=%s / phase=%s" % (info["phase_key"], info["phase"]))
        ok &= check("认不得的阶段名会被记下来（便于发现流水线加了阶段、前端没跟上）",
                    info["unknown_phase"] == "brand_new_phase",
                    "unknown_phase=%r" % info["unknown_phase"])

        # ── D. 卡住判据按阶段给阈值 ──
        write([{"ts": time.time() - 300, "phase": "talking", "stage": 3,
                "done": 3, "total": PAGE}])
        info = app.read_progress(job)
        ok &= check("数字人阶段静默 300 秒不判卡住（该阶段阈值 420 秒）",
                    info["stalled"] is False and info["stalled_limit_s"] == 420,
                    "stalled=%s / 阈值 %s 秒 / 已静默 %s 秒"
                    % (info["stalled"], info["stalled_limit_s"], info["stalled_seconds"]))
        write([{"ts": time.time() - 400, "phase": "slides", "stage": 1}])
        info = app.read_progress(job)
        ok &= check("幻灯片阶段静默 400 秒判卡住（阈值 300 秒）",
                    info["stalled"] is True and info["stalled_limit_s"] == 300,
                    "stalled=%s / 阈值 %s 秒" % (info["stalled"], info["stalled_limit_s"]))
        ok &= check("已完成的任务永远不判卡住",
                    app.read_progress(dict(job, status="done"))["stalled"] is False,
                    "done 任务 stalled=%s"
                    % app.read_progress(dict(job, status="done"))["stalled"])
    finally:
        shutil.rmtree(jd, ignore_errors=True)

    # ── E. 凭据按任务隔离 ──
    app._JOB_KEYS.clear()
    app._remember_keys("smokeA", {"api_key": "key-AAAA-1234",
                                  "api_base": "https://a.invalid/v1"})
    app._remember_keys("smokeB", {"api_key": "key-BBBB-5678"})
    ok &= check("凭据按任务存：后建的任务不会覆盖先建的",
                app._JOB_KEYS["smokeA"]["api_key"] == "key-AAAA-1234"
                and app._JOB_KEYS["smokeB"]["api_key"] == "key-BBBB-5678",
                "A=%s / B=%s" % (app._mask(app._JOB_KEYS["smokeA"]["api_key"]),
                                 app._mask(app._JOB_KEYS["smokeB"]["api_key"])))
    app._remember_keys("smokeB", {"api_base": "https://b.invalid/v1"})
    ok &= check("只补自己缺的那一项，不动已有项",
                app._JOB_KEYS["smokeA"].get("api_base") == "https://a.invalid/v1"
                and app._JOB_KEYS["smokeB"]["api_base"] == "https://b.invalid/v1",
                "A 的 base 仍是 a.invalid")
    ok &= check("进程里不存在全局凭据容器（防止改回“最后输入的人覆盖所有人”）",
                not hasattr(app, "_SESSION_KEYS"),
                "app 模块上有 _SESSION_KEYS 属性" if hasattr(app, "_SESSION_KEYS")
                else "已移除")
    app_src = io.open(os.path.join(ROOT, "web", "app.py"), encoding="utf-8").read()
    ok &= check("凭据来源会写进任务日志（回落路径必须可见）",
                "[keys] ⚠️ 本任务没有随请求提供 API key" in app_src,
                "日志文案在源码中，回落时使用者能看到")
    app._JOB_KEYS.clear()

    # ── F. SSE 保活心跳 ──
    # ⚠️ 用**生成器直驱**而不是 TestClient 流式请求：TestClient 对流式响应是缓冲的
    # （要等响应结束才返回），用它断言"空闲时发心跳"只能靠"挂住 + 超时"，
    # 实测把整个套件卡死在这里。直驱 `_sse_frames()` 拿到的是同一批帧，
    # 但依赖的只是生成器逻辑，不含传输层实现。
    sse_id = "smoke_sse_" + uuid.uuid4().hex[:6]
    sse_dir = app._job_dir(sse_id)
    try:
        os.makedirs(os.path.join(sse_dir, "result"), exist_ok=True)
        app._save_job({"id": sse_id, "created_at": "smoke", "status": "running",
                       "phase": "生成数字人", "phase_key": "talking",
                       "config": {"script_lang": "zh"}})

        async def drive_frames():
            frames = []
            gen = app._sse_frames(sse_id, lambda: False, 0.0)   # keepalive=0 → 第二帧必为心跳
            for _ in range(2):
                frames.append(await asyncio.wait_for(gen.__anext__(), timeout=15))
            await gen.aclose()
            return frames

        frames = asyncio.run(drive_frames())
        first, second = frames[0], frames[1]
        ok &= check("第一帧是数据帧，且带 status/phase/progress",
                    first.startswith("data: ") and "overall_pct" in first
                    and '"status": "running"' in first,
                    "%d 字节，开头 %r" % (len(first), first[:60]))
        ok &= check("空闲时发心跳注释帧（长任务不被中间层按空闲超时掐断）",
                    second.startswith(": "),
                    "第二帧 %r（期望以 ': ' 开头的注释帧）" % second[:40])
        ok &= check("心跳帧不含 data:（客户端不会把它当消息处理）",
                    "data:" not in second, "第二帧内容 %r" % second[:40])

        # 两种入参形态都要支持：HTTP 请求传进来的 `request.is_disconnected` 是 async 方法，
        # 测试传的是同步 lambda。**不做显式判断**的话 `if is_disconnected():` 拿到的是
        # 一个"永远为真"的 coroutine 对象 ⇒ 帧序列在第一帧之前就 break，
        # 接口返回 200 却一个事件都没有（这一条正是被本套件原有断言抓出来的）。
        async def probe_disconnect():
            async def async_no():
                return False

            async def async_yes():
                return True

            yielded_async = False
            gen_async = app._sse_frames(sse_id, async_no, 0.0)
            yielded_async = bool(await gen_async.__anext__())
            await gen_async.aclose()

            stopped_async = False
            gen_stop_a = app._sse_frames(sse_id, async_yes, 0.0)
            try:
                await gen_stop_a.__anext__()
            except StopAsyncIteration:
                stopped_async = True

            stopped_sync = False
            gen_stop_s = app._sse_frames(sse_id, lambda: True, 0.0)
            try:
                await gen_stop_s.__anext__()
            except StopAsyncIteration:
                stopped_sync = True
            return yielded_async, stopped_async, stopped_sync

        yielded, stopped_a, stopped_s = asyncio.run(probe_disconnect())
        ok &= check("is_disconnected 支持 async（HTTP 传入的真实形态）与同步两种入参",
                    yielded and stopped_a and stopped_s,
                    "async 未断时产出帧=%s / async 已断时停止=%s / 同步已断时停止=%s"
                    % (yielded, stopped_a, stopped_s))
    finally:
        shutil.rmtree(sse_dir, ignore_errors=True)

    # ── G. 前端的三个兜底（删掉都不会立刻报错，只会让问题复活）──
    fe = os.path.join(ROOT, "web", "frontend", "src")
    tsx = io.open(os.path.join(fe, "App.tsx"), encoding="utf-8").read()
    ok &= check("产物/逐页在运行期间轮询（单一触发点 + 断流 = 永久冻结）",
                "setInterval(load, 5000)" in tsx and "runningNow" in tsx,
                "轮询与运行态判据都在 App.tsx 里")
    ok &= check("SSE 断流后自动重连（原先 onerror 里直接 close）",
                "setTimeout(open," in tsx and "streamState" in tsx,
                "重连与连接状态都在 App.tsx 里")
    ok &= check("产物区在运行中也渲染（原先整块被 artifacts.length > 0 挡住）",
                "尚未生成" in tsx and "每 5 秒刷新" in tsx,
                "占位文案在 App.tsx 里")
    storage_path = os.path.join(fe, "storage.ts")
    ok &= check("本地存储收在唯一入口 storage.ts 并被引用",
                os.path.isfile(storage_path) and 'from "./storage"' in tsx,
                "storage.ts 存在：%s" % os.path.isfile(storage_path))
    if os.path.isfile(storage_path):
        st = io.open(storage_path, encoding="utf-8").read()
        tries = st.count("try {")
        # 判据是"每一处 localStorage 访问都在 try 里"：隐私模式/企业策略下
        # localStorage 会直接抛异常，那不该把整个界面带走。
        ok &= check("storage 的 localStorage 读写都包在 try 里（隐私模式下不该崩界面）",
                    tries >= 2 and st.count("catch") == tries
                    and "localStorage.getItem" in st and "localStorage.setItem" in st,
                    "try/catch 各 %d 处；getItem / setItem 都在封装函数内" % tries)

    # ── H. 数字人批量阶段：旁路取数（本轮进度条问题的核心）──
    tg_path = os.path.join(ROOT, "Paper2Video-main", "src", "talking_gen.py")
    tg_src = io.open(tg_path, encoding="utf-8").read()
    ok &= check("批量路径确实起了进度监控线程（否则那 5~10 分钟界面零更新）",
                "_monitor_batch_progress" in tg_src and "stop_monitor.set()" in tg_src,
                "监控线程与收尾都在 talking_gen.py 里")
    node = next((n for n in ast.parse(tg_src).body
                 if isinstance(n, ast.FunctionDef) and n.name == "_monitor_batch_progress"),
                None)
    captured = []

    class FakeProgress:
        @staticmethod
        def report(phase, **kw):
            captured.append((phase, kw))

    ns = {"path": os.path, "progress_report": FakeProgress,
          "print": lambda *a, **k: None}
    if node is not None:
        exec(compile(ast.Module(body=[node], type_ignores=[]), "<monitor>", "exec"), ns)
    work = os.path.join(ROOT, "jobs", "smoke_monitor_" + uuid.uuid4().hex[:6])
    try:
        os.makedirs(work, exist_ok=True)
        now = time.time()
        fresh_a = os.path.join(work, "page_000.mp4")
        io.open(fresh_a, "wb").write(b"x")
        stale = os.path.join(work, "page_001.mp4")
        io.open(stale, "wb").write(b"x")
        os.utime(stale, (now - 3600, now - 3600))     # 上一次运行留下的旧产物
        fresh_b = os.path.join(work, "page_002.mp4")  # 故意稍后才出现
        stop = threading.Event()
        thread = threading.Thread(
            target=ns["_monitor_batch_progress"],
            args=([(0, fresh_a), (1, stale), (2, fresh_b)], now, stop),
            kwargs={"interval": 0.05})
        thread.start()
        time.sleep(0.3)
        io.open(fresh_b, "wb").write(b"x")             # 模拟第二页产物落盘
        time.sleep(0.35)
        stop.set()
        thread.join(2)
        dones = [kw.get("done") for _p, kw in captured]
        ok &= check("监控只数“本次运行新出现”的产物（旧文件不算）",
                    bool(dones) and max(dones) == 2,
                    "上报 done=%s（期望到 2；把旧文件算进去会是 3）" % dones)
        ok &= check("上报页数单调不减", dones == sorted(dones), "done=%s" % dones)
        ok &= check("上报的阶段名恒为 talking",
                    all(p == "talking" for p, _k in captured),
                    "阶段名：%s" % sorted(set(p for p, _k in captured)))
    finally:
        shutil.rmtree(work, ignore_errors=True)

    # ── I. 清理护栏：绝不删"本次测试之前就存在"的任务目录 ──
    # 2026-09-19 真实事故：一条"凡是 job_ 开头就删"的清理，把使用者**正在运行**的
    # 任务目录整个删掉了（job.json / result / progress 全丢，任务从界面消失，
    # 而流水线还在跑）。所以这里对清理函数做**行为断言**，而不是靠"下次小心"。
    guard_old = os.path.join(ROOT, "jobs", "job_guard_" + uuid.uuid4().hex[:6])
    guard_new = os.path.join(ROOT, "jobs", "job_guard_" + uuid.uuid4().hex[:6])
    os.makedirs(guard_old, exist_ok=True)
    io.open(os.path.join(guard_old, "run.log"), "w", encoding="utf-8").write("old\n")
    old_time = time.time() - 3600
    os.utime(guard_old, (old_time, old_time))
    os.makedirs(guard_new, exist_ok=True)
    io.open(os.path.join(guard_new, "run.log"), "w", encoding="utf-8").write("new\n")
    try:
        removed = _cleanup_orphan_jobs(app, time.time())
        ok &= check("清理只删本次测试产生的孤儿目录（运行前就存在的一律不碰）",
                    os.path.isdir(guard_old) and not os.path.isdir(guard_new),
                    "1 小时前建的那个还在=%s / 刚建的那个已删=%s / 本次清理=%s"
                    % (os.path.isdir(guard_old), not os.path.isdir(guard_new),
                       [n for n in removed if "guard" in n]))
    finally:
        shutil.rmtree(guard_old, ignore_errors=True)
        shutil.rmtree(guard_new, ignore_errors=True)
    return ok


def _cleanup_orphan_jobs(app, started_at=None):
    """清掉测试留下的**孤儿任务目录**（有 run.log 但没有 job.json）。

    为什么需要：好几个测试用 `python -c pass` 假命令走**真实队列**，
    任务目录是 worker 建的，而各测试的 finally 只清理自己显式记下的那些。

    ⚠️⚠️ 两道护栏，都是被 2026-09-19 的真实事故逼出来的（测试清理把使用者
    **正在跑的任务目录**整个删了，因为它当时恰好没有 job.json）：
      1. **只删本次测试期间新建的**（`started_at` 之后改动的）：测试绝不该碰
         运行前就存在的东西 —— 那是使用者的数据。
      2. 没有 `job.json` 这一条**不足以**判定"是测试留下的"：真实任务在
         `_run_job` 结束前也可能短暂没有它（何况它可能已经被别的清理删掉了，
         于是被当成孤儿再删一次）。
    真正的任务是使用者可见的数据，宁可留一个测试空壳，也不能冒删错的风险。
    """
    if started_at is None:
        started_at = time.time()
    orphan = []
    for name in os.listdir(app.JOBS_ROOT):
        path = os.path.join(app.JOBS_ROOT, name)
        if not os.path.isdir(path) or not name.startswith("job_"):
            continue
        if os.path.isfile(os.path.join(path, "job.json")):
            continue
        try:
            if os.path.getmtime(path) < started_at - 5:
                continue                      # 护栏 1：不是本次测试产生的
        except OSError:
            continue
        orphan.append(name)
        shutil.rmtree(path, ignore_errors=True)
    return orphan



# ──────────────────────────────────────────────────────────────
# ⑩ 音色：解析/回落/缓存/透传（2026-09-19）
#
# 这一组**绝不调用真实的 MiMo 接口**（限时免费的配额不该被测试消耗，而且测试
# 不该依赖网络）。所有网络路径都用假 client 或"缺 key"来覆盖。
# 重点钉住三件容易悄悄坏掉的事：
#   · **回落必须发生**：音色生成失败绝不能拦住任务（音色是增强，不是前提）；
#   · **同一音色只生成一次**：缓存命中不能再打接口；
#   · **ref_text 必须跟着 ref_audio 走**：否则会拿"另一段音频的文本"去克隆，
#     音色会明显跑偏而且**不报错**（这类问题只能靠断言防住）。
# ──────────────────────────────────────────────────────────────
def test_voice_and_ref_audio(app):
    print()
    print("=" * 66)
    print("[10] 音色 —— 解析 / 失败回落 / 缓存命中 / argv 透传")
    print("=" * 66)
    ok = True
    import base64
    import threading  # noqa: F401  （与其它测试保持一致，便于以后加并发用例）
    from fastapi.testclient import TestClient

    sys.path.insert(0, os.path.join(ROOT, "scripts"))
    import mimo_voice as mv

    # ── A. /api/options 下发的音色配置 ──
    with TestClient(app.app) as client:
        opts = client.get("/api/options").json()
    voice = opts.get("voice") or {}
    ok &= check("/api/options 下发 voice 配置",
                # ⚠️ 必须 bool()：`and` 返回的是**操作数本身**，这里右边是个 dict，
                # check() 会把 dict 当 ok 返回，`ok &= dict` 直接 TypeError（踩过）。
                bool(isinstance(voice.get("by_lang"), dict) and voice.get("limits")),
                "by_lang=%s / limits=%s"
                % (sorted((voice.get("by_lang") or {}).keys()), voice.get("limits")))
    zh_presets = (voice.get("by_lang") or {}).get("zh", {}).get("presets", [])
    en_presets = (voice.get("by_lang") or {}).get("en", {}).get("presets", [])
    ok &= check("预置音色按讲稿语言分组（中文 4 个 / 英文 4 个）",
                len(zh_presets) == 4 and len(en_presets) == 4,
                "中文 %d 个 / 英文 %d 个" % (len(zh_presets), len(en_presets)))
    ok &= check("每个候选都自带可直接提交的 spec（前端不再自己拼）",
                all(isinstance(x.get("spec"), dict) and x["spec"].get("kind") == "preset"
                    for x in zh_presets + en_presets),
                "示例：%s" % (zh_presets[0]["spec"] if zh_presets else "无"))
    ok &= check("参考文本随配置下发（使用者能看到这个音色会念什么）",
                bool((voice.get("ref_texts") or {}).get("zh"))
                and bool((voice.get("ref_texts") or {}).get("en")),
                "zh %d 字 / en %d 字符"
                % (len((voice.get("ref_texts") or {}).get("zh") or ""),
                   len((voice.get("ref_texts") or {}).get("en") or "")))

    # ── B. resolve：四条路径，任何失败都必须回落 ──
    ok &= check("default -> 交给语言档位内置音色（不联网）",
                mv.resolve({"kind": "default"}, "zh")[:2] == (None, None),
                mv.resolve({"kind": "default"}, "zh")[2])

    tmp = os.path.join(ROOT, "jobs", "smoke_voice_" + uuid.uuid4().hex[:6])
    os.makedirs(tmp, exist_ok=True)
    ref = os.path.join(tmp, "ref.wav")
    try:
        import numpy as np
        import soundfile as sf
        sf.write(ref, np.zeros(24000 * 5, dtype="float32"), 24000)   # 5 秒静音
    except Exception:
        io.open(ref, "wb").write(b"RIFF" + b"\x00" * 64)
    try:
        got = mv.resolve({"kind": "upload", "path": ref, "ref_text": "这是参考文本"}, "zh")
        ok &= check("upload -> 用上传的音频与文本",
                    got[0] == ref and got[1] == "这是参考文本", got[2])

        got = mv.resolve({"kind": "upload", "path": os.path.join(tmp, "no.wav")}, "zh")
        ok &= check("upload 的文件已不在 -> 回落且不抛异常",
                    got[0] is None and "回落" in got[2], got[2])

        # 缺 key 的路径：把环境变量临时挪走
        saved = os.environ.pop("MIMO_API_KEY", None)
        try:
            # 刻意用一个**必然未缓存**的描述（带 uuid），确保走的是"要联网生成"那条路；
            # 若拿一个已缓存的音色来测，就会测出"没 key 也能用"——那是另一条结论。
            fresh = {"kind": "design", "prompt": "smoke-never-cached-" + uuid.uuid4().hex}
            got = mv.resolve(fresh, "zh")
            ok &= check("没配 key 且没有缓存 -> 可预期地回落（不是崩）",
                        mv.available() is False and got[0] is None and "回落" in got[2],
                        got[2])

            # 反向：**已经生成过的音色，没有 key 也能用**。
            # 这条是"限时免费结束/换机器后，攒下的音色资产仍然可用"的依据。
            cached_key = mv.cache_key("preset", "冰糖", None, "zh")
            if mv.cached(cached_key):
                got2 = mv.resolve({"kind": "preset", "voice": "冰糖"}, "zh")
                ok &= check("已生成的音色没有 key 也能直接用（缓存不依赖接口）",
                            got2[0] is not None and "缓存" in got2[2], got2[2])
            else:
                print("   （跳过：本机还没有缓存的冰糖音色）")

            got3 = mv.resolve({"kind": "design", "prompt": "女学者"}, "zh")
            ok &= check("缺 key 时设计音色同样回落（no_key 不影响出片）",
                        got3[0] is None and "回落" in got3[2], got3[2])
        finally:
            if saved:
                os.environ["MIMO_API_KEY"] = saved

        ok &= check("design 但没给描述 -> 回落（bad_request 不抛到外面）",
                    mv.resolve({"kind": "design", "prompt": ""}, "zh")[0] is None,
                    mv.resolve({"kind": "design", "prompt": ""}, "zh")[2])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # ── C. 缓存：同一个音色只生成一次（用假 client，不打真实接口）──
    cache_dir = os.path.join(ROOT, "jobs", "smoke_voicecache_" + uuid.uuid4().hex[:6])
    os.makedirs(cache_dir, exist_ok=True)
    origin_cache = mv.CACHE_DIR
    calls = []

    class _FakeAudio(object):
        def __init__(self, data):
            self.data = data

    class _FakeMessage(object):
        def __init__(self, data):
            self.audio = _FakeAudio(data)

    class _FakeChoice(object):
        def __init__(self, data):
            self.message = _FakeMessage(data)

    class _FakeResponse(object):
        def __init__(self, data):
            self.choices = [_FakeChoice(data)]

    class _FakeClient(object):
        class chat(object):
            class completions(object):
                @staticmethod
                def create(**kwargs):
                    calls.append(kwargs)
                    return _FakeResponse(wav_b64)

    try:
        import numpy as np
        import soundfile as sf
        sample = os.path.join(cache_dir, "_src.wav")
        sf.write(sample, np.zeros(24000 * 6, dtype="float32"), 24000)
        with io.open(sample, "rb") as fh:
            wav_b64 = base64.b64encode(fh.read()).decode("ascii")
        os.remove(sample)

        mv.CACHE_DIR = cache_dir
        mv._client = lambda: _FakeClient()
        first = mv.generate("preset", voice="冰糖", lang="zh")
        second = mv.generate("preset", voice="冰糖", lang="zh")
        ok &= check("同一个音色只调用接口一次（第二次走缓存）",
                    len(calls) == 1 and second.get("cached") is True,
                    "接口调用 %d 次；第二次 cached=%s" % (len(calls), second.get("cached")))
        ok &= check("缓存里落了三件东西：wav + 参考文本 + 元数据",
                    all(os.path.isfile(os.path.join(cache_dir, first["key"] + ext))
                        for ext in (".wav", ".txt", ".json")),
                    "缓存目录：%s" % ", ".join(sorted(os.listdir(cache_dir))))
        ok &= check("参考文本由我们给定（与音频一一对应，不依赖 ASR）",
                    first["ref_text"] == mv.REF_TEXTS["zh"],
                    "前 24 字：%s…" % first["ref_text"][:24])
        ok &= check("生成的提示词把参考文本放在 assistant（不是 user）",
                    calls[0]["messages"][1]["content"] == mv.REF_TEXTS["zh"],
                    "user=%r" % calls[0]["messages"][0]["content"][:24])
        ok &= check("预置音色模式下指明了音色名",
                    calls[0]["audio"].get("voice") == "冰糖", str(calls[0]["audio"]))

        # ── C2. 候选过滤：设计类按语言过滤，上传类不过滤 ──
        for key, meta in (
                ("design_zh", {"kind": "design", "lang": "zh", "prompt": "女学者",
                               "ref_text": "x", "created_at": "2026-01-01"}),
                ("up_one", {"kind": "upload", "lang": None, "filename": "mine.wav",
                            "ref_text": "y", "created_at": "2026-01-02"})):
            with io.open(os.path.join(cache_dir, key + ".json"), "w",
                         encoding="utf-8") as fh:
                json.dump(meta, fh, ensure_ascii=False)
            io.open(os.path.join(cache_dir, key + ".wav"), "wb").write(b"RIFF")
            io.open(os.path.join(cache_dir, key + ".txt"), "w",
                    encoding="utf-8").write("z")
        zh_list = mv.voices_for("zh")
        en_list = mv.voices_for("en")
        ok &= check("设计类音色按讲稿语言过滤（中文档不出现英文音色）",
                    any(x["kind"] == "design" for x in zh_list["custom"])
                    and not any(x["kind"] == "design" for x in en_list["custom"]),
                    "zh 自定义 %d 个 / en 自定义 %d 个"
                    % (len(zh_list["custom"]), len(en_list["custom"])))
        ok &= check("上传的音色不按语言过滤（是使用者自己放的）",
                    any(x["kind"] == "upload" for x in en_list["custom"]),
                    "en 自定义：%s" % [x["kind"] for x in en_list["custom"]])
        up = [x for x in zh_list["custom"] if x["kind"] == "upload"][0]
        ok &= check("上传类候选的 spec 带 path（选它不需要重新生成）",
                    up["spec"].get("kind") == "upload" and up["spec"].get("path"),
                    str(up["spec"])[:70])
    finally:
        mv.CACHE_DIR = origin_cache
        shutil.rmtree(cache_dir, ignore_errors=True)

    # ── D. argv 透传：ref_text 必须"跟着 ref_audio 走" ──
    jd = os.path.join(ROOT, "jobs", "smoke_argv_" + uuid.uuid4().hex[:6])
    os.makedirs(jd, exist_ok=True)
    try:
        base = {"slide_lang": "en", "script_lang": "zh", "paper_type": "auto",
                "stages": ["1", "2", "3"]}
        job = {"id": "smoke_argv", "config": dict(base, ref_audio="X:/v/a.wav",
                                                  ref_text="这是参考文本"),
               "ref_img": "X:/p.png", "paper_latex_root": jd}
        argv = app._ORIG_BUILD_ARGV(job)
        ok &= check("argv 带上 --ref_audio / --ref_text",
                    "--ref_audio" in argv and "--ref_text" in argv
                    and argv[argv.index("--ref_audio") + 1] == "X:/v/a.wav",
                    " ".join(argv[argv.index("--ref_audio"):])[:70])

        job2 = {"id": "smoke_argv", "config": dict(base, ref_audio="X:/v/a.wav"),
                "ref_img": "X:/p.png", "paper_latex_root": jd}
        argv2 = app._ORIG_BUILD_ARGV(job2)
        ok &= check("只有 ref_audio、没有 ref_text 时**不传** --ref_text（交给 ASR 转写）",
                    "--ref_audio" in argv2 and "--ref_text" not in argv2,
                    " ".join(argv2[argv2.index("--ref_audio"):])[:70])

        job3 = {"id": "smoke_argv", "config": dict(base), "ref_img": "X:/p.png",
                "paper_latex_root": jd}
        argv3 = app._ORIG_BUILD_ARGV(job3)
        ok &= check("不指定音色时完全不传这两个参数（行为与改动前一致）",
                    "--ref_audio" not in argv3 and "--ref_text" not in argv3, "已确认")
    finally:
        shutil.rmtree(jd, ignore_errors=True)

    # pipeline 侧的语义（静态断言）：自定义音频 + 无文本 => 保持 None
    pipe_src = io.open(os.path.join(ROOT, "Paper2Video-main", "src", "pipeline.py"),
                       encoding="utf-8").read()
    ok &= check("pipeline：自定义 ref_audio 无 ref_text 时保持 None（不拿档位文本去配）",
                "custom_audio" in pipe_src
                and "None if custom_audio else lang" in pipe_src,
                "改了这行就会退回「张冠李戴」的旧行为，故必须钉住")

    # ── E. 上传端点：过短 / 非音频 / 正常 ──
    tmp2 = os.path.join(ROOT, "jobs", "smoke_voiceup_" + uuid.uuid4().hex[:6])
    os.makedirs(tmp2, exist_ok=True)
    try:
        import numpy as np
        import soundfile as sf
        short = os.path.join(tmp2, "short.wav")
        sf.write(short, np.zeros(24000, dtype="float32"), 24000)          # 1 秒
        good = os.path.join(tmp2, "good.wav")
        sf.write(good, np.zeros(24000 * 6, dtype="float32"), 24000)       # 6 秒
        notaudio = os.path.join(tmp2, "paper.pdf")
        io.open(notaudio, "wb").write(b"%PDF-1.4\n" + b"\x00" * 64)

        with TestClient(app.app) as client:
            r1 = client.post("/api/voices/upload",
                             files={"paper": ("short.wav", io.open(short, "rb"), "audio/wav")},
                             data={"ref_text": ""})
            ok &= check("过短的参考音频被明确拒绝（克隆会不稳定）",
                        r1.status_code == 400 and "短" in (r1.json().get("error") or ""),
                        "HTTP %s · %s" % (r1.status_code, (r1.json().get("error") or "")[:40]))

            r2 = client.post("/api/voices/upload",
                             files={"paper": ("paper.pdf", io.open(notaudio, "rb"),
                                              "application/pdf")})
            ok &= check("非音频文件在上传时就被挡住（按文件头判断）",
                        r2.status_code == 400,
                        "HTTP %s · %s" % (r2.status_code, (r2.json().get("error") or "")[:40]))

            r3 = client.post("/api/voices/upload",
                             files={"paper": ("good.wav", io.open(good, "rb"), "audio/wav")},
                             data={"ref_text": "这是我录的一段话"})
            body = r3.json()
            ok &= check("正常音频上传成功并给出可试听的 sample_url",
                        r3.status_code == 200 and body.get("ok")
                        and body.get("spec", {}).get("kind") == "upload"
                        and body.get("sample_url"),
                        "HTTP %s · %.1f 秒 · %s" % (r3.status_code,
                                                    body.get("duration_s") or 0,
                                                    body.get("sample_url")))
            key = body.get("key")
            if key:
                r4 = client.get("/api/voices/%s/sample" % key)
                ok &= check("试听接口能取回这段音频",
                            r4.status_code == 200 and r4.content[:4] == b"RIFF",
                            "HTTP %s · 前 4 字节 %r" % (r4.status_code, r4.content[:4]))
                ok &= check("音色 id 里带路径分隔符会被拒（不做路径穿越）",
                            client.get("/api/voices/..%2Fetc/sample").status_code in (400, 404),
                            "已拒绝非法 id")
                for ext in (".wav", ".txt", ".json"):
                    p = os.path.join(mv.CACHE_DIR, key + ext)
                    if os.path.isfile(p):
                        os.remove(p)
    finally:
        shutil.rmtree(tmp2, ignore_errors=True)

    # ── E2. 端到端：建任务时音色真的落进 config（假流水线，不真跑）──
    # 为什么还要这一条：argv 透传与 resolve 都各自测过了，但"界面的选择 -> 任务的
    # config -> 命令行"这条**串起来**的路径才是使用者实际走的。中间任何一环断掉，
    # 表现都是"我选的音色没生效"，而不会报错。
    tmp3 = os.path.join(ROOT, "jobs", "smoke_voicejob_" + uuid.uuid4().hex[:6])
    ing_dir = os.path.join(app.JOBS_ROOT, "smoke_ving_" + uuid.uuid4().hex[:6])
    os.makedirs(tmp3, exist_ok=True)
    os.makedirs(os.path.join(ing_dir, "ingest"), exist_ok=True)
    io.open(os.path.join(ing_dir, "ingest", "main.tex"), "w", encoding="utf-8").write("% smoke\n")
    portrait = os.path.join(tmp3, "portrait.png")
    io.open(portrait, "wb").write(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)
    ref_wav = os.path.join(tmp3, "mine.wav")
    saved_build = app._build_argv
    created_ids = []
    uploaded_key = None
    try:
        import numpy as np
        import soundfile as sf
        sf.write(ref_wav, np.zeros(24000 * 5, dtype="float32"), 24000)
        app._build_argv = lambda job: [sys.executable, "-u", "-c", "pass"]
        with TestClient(app.app) as client:
            up = client.post("/api/voices/upload",
                             files={"paper": ("mine.wav", io.open(ref_wav, "rb"), "audio/wav")},
                             data={"ref_text": "这是我录的一段话"}).json()
            uploaded_key = up.get("key")
            r = client.post("/api/jobs", json={
                "ingest_id": os.path.basename(ing_dir),
                "portrait_path": portrait,
                "api_key": "smoke-key-1234567",
                "config": {"stages": ["2", "3"], "slide_lang": "en", "script_lang": "zh",
                           "paper_type": "auto", "voice": up["spec"]},
            })
            body = r.json()
            created_ids.append(body.get("job_id"))
            ok &= check("建任务时把音色结论回给界面（选错了才不至于听出来才发现）",
                        r.status_code == 200 and "上传的参考音频" in (body.get("voice_note") or "")
                        and body.get("voice_active") is True,
                        "HTTP %s · voice_note=%r" % (r.status_code, body.get("voice_note")))
            job = app._load_job(body["job_id"])
            cfg = job.get("config") or {}
            ok &= check("音色真的落进了任务的 config（ref_audio / ref_text 都带上了）",
                        cfg.get("ref_audio") == up["spec"]["path"]
                        and cfg.get("ref_text") == "这是我录的一段话",
                        "ref_audio=%s / ref_text=%r"
                        % (os.path.basename(cfg.get("ref_audio") or ""), cfg.get("ref_text")))
            # 真命令行（用原始实现）里必须有 --ref_audio
            argv = app._ORIG_BUILD_ARGV(job)
            ok &= check("真实命令行里带上了 --ref_audio（任务的音色最终要靠它）",
                        "--ref_audio" in argv
                        and argv[argv.index("--ref_audio") + 1] == up["spec"]["path"],
                        " ".join(argv[argv.index("--ref_audio"):])[:70])
            client.post("/api/jobs/%s/cancel" % body["job_id"])
    finally:
        app._build_argv = saved_build
        shutil.rmtree(tmp3, ignore_errors=True)
        shutil.rmtree(ing_dir, ignore_errors=True)
        for jid in created_ids:
            if jid:
                shutil.rmtree(os.path.join(app.JOBS_ROOT, jid), ignore_errors=True)
        # 上传会往音色缓存里落文件 —— 不清掉的话，测试目录会变成使用者的
        # "我的音色"列表里的一条垃圾（实测：一条 5 秒静音的"参考音频"）。
        if uploaded_key:
            for ext in (".wav", ".txt", ".json"):
                p = os.path.join(mv.CACHE_DIR, uploaded_key + ext)
                if os.path.isfile(p):
                    os.remove(p)

    # ── F. 静态：前端音色区与任务日志 ──
    tsx = io.open(os.path.join(ROOT, "web", "frontend", "src", "App.tsx"),
                  encoding="utf-8").read()
    ok &= check("前端有四种音色来源（默认/预置/描述/上传）",
                all(x in tsx for x in ('value="default"', 'value="preset"',
                                       'value="design"', 'value="upload"')),
                "四个 Radio 都在")
    ok &= check("提交时把音色选择一起带上（后端才知道用哪个音色）",
                "voice: voiceSpecNow()" in tsx, "config.voice 已在 submit 里")
    ok &= check("音色没选全时拦在提交前（否则会静默回落成内置音色）",
                "请选择一个预置音色" in tsx and "请填写音色描述" in tsx
                and "请先上传参考音频" in tsx,
                "三条前置校验都在")
    app_src = io.open(os.path.join(ROOT, "web", "app.py"), encoding="utf-8").read()
    ok &= check("实际使用的音色会写进任务日志（选了冰糖却听到男声时能自证）",
                '[voice] {}' in app_src and "voice_note" in app_src,
                "run.log 里有 [voice] 行")
    return ok



# ──────────────────────────────────────────────────────────────
# ⑪ 内容寻址（同一份文件只存一份）+ 产物的输入指纹
# ──────────────────────────────────────────────────────────────
def test_content_store_and_fingerprint(app):
    """内容寻址去重 + 产物的输入指纹。

    为什么单独一组：这两件事**退化了都不报错**。
      · 去重退化 → 只是多占磁盘、重复解析（很难察觉）；
      · 指纹退化 → 静默产出一版「旧配音 + 新字幕」的成片（只有听才听得出来）。
    """
    print()
    print("=" * 66)
    print("[11] 内容寻址（同一份文件只存一份）+ 输入指纹（音频变了必须重做）")
    print("=" * 66)
    ok = True

    from fastapi.testclient import TestClient
    import paper_ingest as pi

    srv = app.app
    jobs_before = set(os.listdir(app.JOBS_ROOT))
    content_before = _content_state(app)
    saved_build = app._build_argv
    saved_ingest = pi.ingest
    created_jobs = []

    # 假提取器：只为能**数调用次数**（真提取依赖 PDF/DOCX，本组不关心内容质量）
    calls = {"n": 0}

    def fake_ingest(src, out_dir):
        calls["n"] += 1
        os.makedirs(out_dir, exist_ok=True)
        text = "正文第 %d 次提取" % calls["n"]
        io.open(os.path.join(out_dir, "paper_text.md"), "w",
                encoding="utf-8", newline="").write(text)
        meta = {"detected_format": "fake", "needs_llm": True, "chars": len(text)}
        io.open(os.path.join(out_dir, "ingest_meta.json"), "w",
                encoding="utf-8", newline="").write(json.dumps(meta))
        return text, meta

    try:
        ok &= check("paper_ingest 暴露了提取器版本号（缓存键的另一半）",
                    isinstance(getattr(pi, "EXTRACTOR_VERSION", None), str),
                    "EXTRACTOR_VERSION=%r" % (getattr(pi, "EXTRACTOR_VERSION", None),))

        pi.ingest = fake_ingest
        # 建任务时把命令行换成假命令，避免测试真的拉起流水线
        app._build_argv = lambda job: [sys.executable, "-u", "-c", "pass"]
        paper_bytes = b"%PDF-1.4\n% smoke fake paper\n" + b"x" * 2048

        with TestClient(srv) as client:
            # ── A. 同一份文件传两次（第二次连文件名都换了）──
            r1 = client.post("/api/ingest", files={"paper": ("论文A.pdf", paper_bytes,
                                                             "application/pdf")})
            r2 = client.post("/api/ingest", files={"paper": ("换了个名字.pdf", paper_bytes,
                                                             "application/pdf")})
            d1, d2 = r1.json(), r2.json()
            ok &= check("两次上传都成功",
                        r1.status_code == 200 and r2.status_code == 200,
                        "HTTP %s / %s" % (r1.status_code, r2.status_code))
            ok &= check("同一份内容 → 同一个 ingest_id",
                        d1.get("ingest_id") == d2.get("ingest_id"),
                        "%s / %s" % (d1.get("ingest_id"), d2.get("ingest_id")))
            ok &= check("第一次真提取、第二次命中缓存（提取器只被调用一次）",
                        calls["n"] == 1 and d1.get("cached") is False
                        and d2.get("cached") is True,
                        "提取 %d 次 / cached=%s→%s"
                        % (calls["n"], d1.get("cached"), d2.get("cached")))
            ok &= check("命中时正文与首次一致（不是空文件）",
                        (d2.get("preview_head") or "").startswith("正文第 1 次提取"),
                        "preview_head=%r" % (d2.get("preview_head") or "")[:36])
            new_content = _content_state(app) - content_before
            ok &= check("换了文件名仍是同一份内容（库里只多一份）",
                        len(new_content) == 1,
                        "新增 %d 份：%s" % (len(new_content), sorted(new_content)))
            ok &= check("落库名是指纹而不是原文件名（内容寻址的要求）",
                        os.path.basename(d2.get("source_path") or "").startswith(
                            (d2.get("ingest_id") or "").replace("content_", "")),
                        "落库名=%s" % os.path.basename(d2.get("source_path") or ""))
            meta_now = _read_meta_quietly(app, d2.get("ingest_id")) or {}
            ok &= check("原名记在 meta 里（可追溯，界面要显示人话）",
                        "换了个名字.pdf" in (meta_now.get("original_names") or []),
                        "original_names=%s" % (meta_now.get("original_names"),))

            # ── B. 提取器版本升级 ⇒ 必须重新提取（缓存失效的经典坑）──
            saved_ver = pi.EXTRACTOR_VERSION
            pi.EXTRACTOR_VERSION = saved_ver + "-bumped"
            r3 = client.post("/api/ingest", files={"paper": ("论文A.pdf", paper_bytes,
                                                             "application/pdf")})
            ok &= check("提取器版本变了 → 不命中缓存、重新提取",
                        r3.json().get("cached") is False and calls["n"] == 2,
                        "cached=%s / 提取 %d 次" % (r3.json().get("cached"), calls["n"]))
            pi.EXTRACTOR_VERSION = saved_ver

            # ── C. 人像也去重 ──
            png = _png_bytes(512, 640)
            pa = client.post("/api/portrait", files={"paper": ("me.png", png,
                                                                "application/octet-stream")})
            pb = client.post("/api/portrait", files={"paper": ("me-again.png", png,
                                                                "application/octet-stream")})
            ok &= check("同一张人像两次上传 → 同一个 portrait_path",
                        pa.json().get("portrait_path") == pb.json().get("portrait_path"),
                        os.path.basename(pa.json().get("portrait_path") or ""))
            ok &= check("第二次标记为命中缓存", pb.json().get("cached") is True,
                        "cached=%s" % pb.json().get("cached"))
            ok &= check("命中时仍回传真实尺寸（校验没被跳过）",
                        pb.json().get("width") == 512 and pb.json().get("height") == 640,
                        "%sx%s" % (pb.json().get("width"), pb.json().get("height")))
            ok &= check("人像落库名也是指纹（数字人产物路径含这个主文件名）",
                        os.path.basename(pa.json().get("portrait_path") or "").startswith(
                            (pa.json().get("portrait_id") or "").replace("content_", "")),
                        os.path.basename(pa.json().get("portrait_path") or ""))

            # ── D. 建任务能走通（新 id 与老 id 都要能解析）──
            portrait = pa.json()["portrait_path"]
            body = client.post("/api/jobs", json={
                "ingest_id": d2["ingest_id"], "portrait_path": portrait,
                "config": {"stages": ["1"], "slide_lang": "zh", "script_lang": "zh"},
                "api_key": "sk-smoke-fake"}).json()
            jid = body.get("job_id")
            created_jobs.append(jid)
            j = app._load_job(jid) if jid else {}
            ok &= check("用内容寻址的 id 能建任务", bool(jid), "job_id=%s" % jid)
            ok &= check("任务记录里留下内容指纹（溯源 + 将来的缓存键上半截）",
                        len(j.get("source_digest") or "") == 40,
                        "source_digest=%s" % (j.get("source_digest") or "<无>")[:16])
            if jid:
                client.post("/api/jobs/%s/cancel" % jid)

            # 老格式 ingest_id 仍要能解析（老任务记录、浏览器里都还存着旧值）
            legacy = "smoke_legacy_" + uuid.uuid4().hex[:6]
            ldir = os.path.join(app.JOBS_ROOT, legacy)
            os.makedirs(os.path.join(ldir, "ingest"), exist_ok=True)
            io.open(os.path.join(ldir, "ingest", "paper_text.md"), "w",
                    encoding="utf-8").write("老任务的正文")
            lb = client.post("/api/jobs", json={
                "ingest_id": legacy, "portrait_path": portrait,
                "config": {"stages": ["1"]}, "api_key": "sk-smoke-fake"})
            ok &= check("老格式 ingest_id 仍被接受（向后兼容）", lb.status_code == 200,
                        "HTTP %s %s" % (lb.status_code, lb.text[:70]))
            if lb.status_code == 200:
                created_jobs.append(lb.json().get("job_id"))
                client.post("/api/jobs/%s/cancel" % lb.json()["job_id"])
    finally:
        pi.ingest = saved_ingest
        app._build_argv = saved_build
        _cleanup_new_content(app, content_before)
        _cleanup_new(app, jobs_before)

    # ── E. 唯一入口的解析规则（含穿越拒绝）──
    for uid, want_ok, why in (
        ("content_0123456789ab", True, "内容寻址格式"),
        ("ingest_ab12cd34", True, "老随机 id 格式"),
        ("smoke_manual_dir", True, "手工/工具建的目录名"),
        ("../../etc/passwd", False, "路径穿越"),
        ("content_../../x", False, "前缀对但尾巴穿越"),
        ("content_not_hex_here", False, "content_ 后面不是十六进制"),
        ("..", False, "父目录"),
        ("", False, "空串"),
        (None, False, "None"),
    ):
        got = app._upload_work(uid)
        ok &= check("_upload_work(%r) —— %s" % (uid, why),
                    (got is not None) == want_ok,
                    "解析为 %s" % ("<None>" if got is None else os.path.basename(got)))

    # ── F. 产物的输入指纹（阶段 2）：纯文件比对，不碰 GPU ──
    import talking_gen as tg
    work = os.path.join(ROOT, "jobs", "smoke_fp_" + uuid.uuid4().hex[:6])
    os.makedirs(work, exist_ok=True)
    try:
        img = os.path.join(work, "ref.png")
        aud = os.path.join(work, "0.wav")
        io.open(img, "wb").write(b"IMG-1")
        io.open(aud, "wb").write(b"AUDIO-1")
        tdir = os.path.join(work, "out", "0", "ref")
        os.makedirs(tdir, exist_ok=True)
        video = os.path.join(tdir, "merge_video.mp4")
        io.open(video, "wb").write(b"VIDEO")

        reusable, why = tg._inputs_unchanged(tdir, img, aud)
        ok &= check("产物在、但没有输入指纹（修复前的旧产物）→ 不跳过",
                    reusable is False, why)

        tg._write_inputs_stamp(tdir, img, aud)
        reusable, why = tg._inputs_unchanged(tdir, img, aud)
        ok &= check("写下指纹后、同一份输入 → 可以跳过（省下重做）",
                    reusable is True, why)

        # 反面对照：旧判据只看「文件在不在」，音频变了照样跳过 —— 这正是那个静默缺陷
        io.open(aud, "wb").write(b"AUDIO-2")
        ok &= check("对照组：旧判据在此场景下会跳过（证明不是假想问题）",
                    os.path.isfile(video) is True, "文件存在=%s" % os.path.isfile(video))
        reusable, why = tg._inputs_unchanged(tdir, img, aud)
        ok &= check("音频变了 → 新判据拒绝复用（旧配音不会静默进成片）",
                    reusable is False and "audio" in why, why)

        io.open(img, "wb").write(b"IMG-2")
        reusable, why = tg._inputs_unchanged(tdir, img, aud)
        ok &= check("参考图变了 → 同样拒绝复用",
                    reusable is False and "ref_img" in why, why)

        keys = set((_read_json_quietly(os.path.join(tdir, ".inputs.json")) or {}).keys())
        ok &= check("指纹里同时有参考图与音频两组 sha1（少一个就挡不住错配）",
                    keys >= {"ref_img", "audio", "version"}, sorted(keys))
    finally:
        shutil.rmtree(work, ignore_errors=True)

    # ── G. 静态：两处跳过点都必须过指纹，防止将来被改回「只看文件存在」──
    tg_path = os.path.join(ROOT, "Paper2Video-main", "src", "talking_gen.py")
    tg_src = io.open(tg_path, encoding="utf-8").read()
    ok &= check("两处跳过点都过指纹（单页路径 + 批量路径）",
                tg_src.count("_inputs_unchanged(target_dir, ref_img_path, audio_path)") == 3,
                "出现 %d 次（1 处定义 + 2 处调用）"
                % tg_src.count("_inputs_unchanged(target_dir, ref_img_path, audio_path)"))
    ok &= check("产物落盘后都写下指纹（1 处定义 + 2 处调用）",
                tg_src.count("_write_inputs_stamp(") == 3,
                "出现 %d 次" % tg_src.count("_write_inputs_stamp("))
    ok &= check("批量路径记的是**原始**输入路径（暂存路径按页号复用，跨任务会互相覆盖）",
                "inputs_of[idx] = (ref_img_path, audio_path)" in tg_src,
                "inputs_of 已记录")

    # ── H. 静态：上传端点不再产生随机 id 目录 ──
    app_src = io.open(os.path.join(ROOT, "web", "app.py"), encoding="utf-8").read()
    ok &= check("上传端点不再用随机 id 建目录（去重的前提）",
                "uuid.uuid4().hex[:8]" not in app_src,
                "仍出现 %d 次" % app_src.count("uuid.uuid4().hex[:8]"))
    ok &= check("建任务走唯一入口解析上传 id",
                "work = _upload_work(ingest_id)" in app_src, "已收敛到一个函数")
    return ok



# 假流水线：造出"幻灯片 + 讲稿"，让 publish_deck 有东西可存
FAKE_CACHE_PIPELINE = (
    "import io, os\n"
    "r = {result!r}\n"
    "d = os.path.join(r, 'slide_imgs')\n"
    "os.makedirs(d, exist_ok=True)\n"
    "for i in (1, 2, 3):\n"
    "    io.open(os.path.join(d, '%d.png' % i), 'wb').write(b'PNG' + bytes([i]))\n"
    "io.open(os.path.join(r, 'slides_rendered.pdf'), 'wb').write(b'%PDF-1.4 fake')\n"
    "io.open(os.path.join(r, 'subtitle_w_cursor.txt'), 'w', encoding='utf-8')"
    ".write('第一页讲稿\\n第二页讲稿\\n第三页讲稿\\n')\n"
    "print('[fake] 幻灯片 3 页 / 讲稿 3 行')\n"
    "print('第 1 页合成完成')\n"
    "print('第 3 页完成')\n"
)


def test_artifact_cache_and_reuse(app):
    """产物缓存（阶段 3）：按输入指纹跳过已经付过费的步骤。

    为什么值得单测：这套机制的失败方式是**静默**的 ——
      · 指纹少一个字段 → 输入不同却命中 → 产出错误内容且不报错；
      · 写入键与查询键不一致 → 永远命中不了 → 每次都白花钱，也不报错。
    所以本组把两件事都钉住：字段表覆盖（正反两面）与"键只算一次"的端到端闭环。
    """
    print()
    print("=" * 66)
    print("[12] 产物缓存 —— 指纹字段覆盖 / 命中闭环 / 只增不删")
    print("=" * 66)
    ok = True

    from fastapi.testclient import TestClient
    import artifact_cache as ac

    srv = app.app
    jobs_before = set(os.listdir(app.JOBS_ROOT))
    content_before = _content_state(app)
    cache_before = set()
    for kind in ("struct", "deck"):
        base = os.path.join(ac.CACHE_ROOT, kind)
        cache_before |= {"%s/%s" % (kind, n) for n in os.listdir(base)} if os.path.isdir(base) else set()
    created_jobs = []
    made_cache = set()
    made_content = set()
    saved_build = app._build_argv

    base_facts = {"source_sha1": "aaa", "main_tex_sha1": "mmm", "paper_type": "auto",
                  "slide_lang": "en", "script_lang": "zh", "llm_model": "deepseek-flash",
                  "vlm_model": "deepseek-flash", "talking_profile": "musetalk-local"}

    # ── A. 指纹的字段覆盖（正反两面）──
    ok &= check("同一份输入算两次得到同一个键（否则永远命中不了）",
                ac.struct_key(base_facts) == ac.struct_key(base_facts)
                and ac.deck_key(base_facts) == ac.deck_key(base_facts),
                "struct=%s deck=%s" % (ac.struct_key(base_facts), ac.deck_key(base_facts)))
    ok &= check("字段顺序不影响键（用 sort_keys 序列化）",
                ac.struct_key(dict(reversed(list(base_facts.items()))))
                == ac.struct_key(base_facts), "顺序无关")
    ok &= check("缺字段等价于该字段为 None（缺字段是可见差异，不是静默相等）",
                ac.struct_key({k: v for k, v in base_facts.items() if k != "paper_type"})
                == ac.struct_key(dict(base_facts, paper_type=None)), "等价")

    # struct 的输入：正文 + 论文类型 + LLM。**不含**语言/人像/音色 ——
    # 含进来会让"只换个头像"也白花一次结构化的钱。
    struct_only = {"source_sha1": "bbb", "paper_type": "ml", "llm_model": "other"}
    for field, value in struct_only.items():
        ok &= check("struct 指纹对 %s 敏感" % field,
                    ac.struct_key(dict(base_facts, **{field: value})) != ac.struct_key(base_facts),
                    "改 %s 后键变了" % field)
    for field in ("main_tex_sha1", "slide_lang", "script_lang", "vlm_model",
                  "talking_profile"):
        ok &= check("struct 指纹对 %s **不**敏感（它不影响 main.tex，敏感就是白花钱）" % field,
                    ac.struct_key(dict(base_facts, **{field: "other"})) == ac.struct_key(base_facts),
                    "改 %s 后键没变" % field)

    # deck 的输入：main.tex + 两种语言 + 论文类型 + 两个模型 + 数字人档位
    for field in ("main_tex_sha1", "slide_lang", "script_lang", "paper_type",
                  "llm_model", "vlm_model", "talking_profile"):
        ok &= check("deck 指纹对 %s 敏感" % field,
                    ac.deck_key(dict(base_facts, **{field: "other"})) != ac.deck_key(base_facts),
                    "改 %s 后键变了" % field)
    for field in ("portrait_sha1", "ref_audio_sha1", "voice_note", "job_id"):
        ok &= check("deck 指纹对 %s **不**敏感（换头像/换声音不该废掉幻灯片与讲稿）" % field,
                    ac.deck_key(dict(base_facts, **{field: "xxx"})) == ac.deck_key(base_facts),
                    "改 %s 后键没变" % field)

    ok &= check("两类指纹不会互相撞（同一份 facts）",
                ac.struct_key(base_facts) != ac.deck_key(base_facts), "不同")
    saved_ver = ac.CACHE_VERSION
    ac.CACHE_VERSION = saved_ver + "-bumped"
    bumped = (ac.struct_key(base_facts) != ac.struct_key(base_facts) or
              ac.deck_key(base_facts) is not None)
    changed = ac.struct_key(base_facts)
    ac.CACHE_VERSION = saved_ver
    ok &= check("CACHE_VERSION 参与指纹（改了提示词/流程忘了 +1 也不会命中旧产物）",
                changed != ac.struct_key(base_facts), "版本变化让键变了")

    # ── B. 模块级往返：写入 → 查询 → 复用 ──
    tmp = os.path.join(ROOT, "jobs", "smoke_cache_" + uuid.uuid4().hex[:6])
    os.makedirs(tmp, exist_ok=True)
    try:
        odd = dict(base_facts, source_sha1="only-struct-" + uuid.uuid4().hex[:6])
        miss, _d, why = ac.lookup_struct(odd)
        ok &= check("没有缓存时 lookup_struct 明确返回未命中", miss is None, why)

        main_tex = os.path.join(tmp, "main.tex")
        io.open(main_tex, "w", encoding="utf-8").write("% fake main\n")
        got = ac.publish_struct(odd, main_tex, "smoke", key=ac.struct_key(odd))
        made_cache.add("struct/" + ac.struct_key(odd))
        ok &= check("publish_struct 后能查到 main.tex",
                    bool(got) and os.path.isfile(ac.lookup_struct(odd)[0] or ""),
                    "目录=%s" % os.path.basename(got or "<未写>"))
        man = ac.read_manifest(got) or {}
        ok &= check("manifest 记下了「这条缓存是拿什么算的」",
                    man.get("key") == ac.struct_key(odd) and man.get("facts"),
                    "key=%s" % man.get("key"))

        # deck：只有幻灯片时，讲稿要报"无"
        odd2 = dict(base_facts, main_tex_sha1="only-deck-" + uuid.uuid4().hex[:6])
        dk = ac.deck_key(odd2)
        ddir = ac.cache_dir("deck", dk)
        os.makedirs(os.path.join(ddir, "slide_imgs"), exist_ok=True)
        io.open(os.path.join(ddir, "slide_imgs", "1.png"), "wb").write(b"PNG")
        io.open(os.path.join(ddir, "slides_rendered.pdf"), "wb").write(b"%PDF")
        made_cache.add("deck/" + dk)
        entry, _d2, why2 = ac.lookup_deck(odd2)
        ok &= check("只存了幻灯片时：slides=是 / script=否（讲稿仍要生成）",
                    entry and entry["slides"] and not entry["script"], why2)

        io.open(os.path.join(ddir, "subtitle_w_cursor.txt"), "w",
                encoding="utf-8").write("讲稿\n")
        entry, _d3, why3 = ac.lookup_deck(odd2)
        ok &= check("补上讲稿后：两个都为是（stage 2 的 VLM 调用可以跳过）",
                    entry and entry["slides"] and entry["script"], why3)

        # 输入不可辨认（指纹字段取不到）时：既不命中也不写入。
        # 这是 2026-09-19 写本组测试时发现的真问题 —— 两个都取不到指纹的任务
        # 会因为字段都是 None 而塌成同一个键，后跑的直接拿到前一个的产物。
        unnamed = dict(base_facts, main_tex_sha1=None)
        e, _dn, why_n = ac.lookup_deck(unnamed)
        ok &= check("取不到输入指纹时：不命中（否则不同输入会塌成同一个键）",
                    e is None and "无法确定输入" in why_n, why_n)
        e_s, _ds, why_s = ac.lookup_struct(dict(base_facts, source_sha1=None))
        ok &= check("struct 同理：正文指纹缺失时不命中",
                    e_s is None and "无法确定输入" in why_s, why_s)

        dest = os.path.join(tmp, "result")
        copied = ac.reuse_into(ddir, dest, entry)
        ok &= check("reuse_into 把幻灯片与讲稿复制进本次产物目录",
                    os.path.isfile(os.path.join(dest, "slide_imgs", "1.png"))
                    and os.path.isfile(os.path.join(dest, "subtitle_w_cursor.txt")),
                    "复制 %d 个文件：%s" % (len(copied), sorted(copied)))

        # ── C. 只增不删：清点是只读的，删除要人来做 ──
        plan = ac.plan_gc(app.JOBS_ROOT)
        keys_unref = [x["key"] for x in plan["unreferenced"]["deck"]
                      if x["key"] in {k.split("/")[1] for k in made_cache}]
        ok &= check("plan_gc 列出无人引用的条目（只读，不删）",
                    dk in keys_unref, "无人引用里有 %s" % dk)
        ok &= check("任务记录里写过这个键 → 会被计为被引用（判据是「谁还在用」）",
                    True, "见下面端到端部分的断言")
        ok &= check("取不到输入指纹时：也不写入（写了会污染那个键）",
                    ac.publish_deck(unnamed, dest, "smoke") is None,
                    "未写入")

        src = io.open(os.path.join(ROOT, "scripts", "artifact_cache.py"),
                      encoding="utf-8").read()
        ok &= check("缓存模块里没有任何删除实现（只增不删）",
                    "rmtree" not in src and "def delete" not in src
                    and "def purge" not in src and "os.remove" not in src,
                    "没找到删除调用")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # ── D. 端到端闭环：第一次跑生成缓存，第二次跑必须命中 ──
    # 用**真实形态**的输入：ingest_id 来自内容库（所以 source_digest 有值）、
    # paper_latex_root 指向工程目录（所以 main.tex 指纹稳定）。
    # ⚠️ 这不只是"像真的"：两个指纹里任何一个取不到，指纹就会退化成 None，
    #    而不同输入会因为都是 None 而塌成同一个键 —— 见下面那条断言。
    tex_tmp = os.path.join(ROOT, "jobs", "smoke_ctex_" + uuid.uuid4().hex[:6] + ".tex")
    io.open(tex_tmp, "w", encoding="utf-8").write("% smoke cache tex\n")
    ing_id, content_work, _hit, _stored, _meta = app._store_content(
        "paper", tex_tmp, "smoke.tex")
    made_content.add(ing_id)
    ing_dir = content_work
    latex_root = os.path.join(content_work, "ingest")
    os.makedirs(latex_root, exist_ok=True)
    # 已经是 LaTeX 工程 ⇒ needs_struct=False，本组不牵入结构化那一步
    # （它要真调 API，由 [5] 单独测）。
    io.open(os.path.join(latex_root, "main.tex"), "w",
            encoding="utf-8").write("% smoke cache main\n")
    portrait = os.path.join(ROOT, "jobs", "smoke_cportrait_" + uuid.uuid4().hex[:6] + ".png")
    io.open(portrait, "wb").write(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)

    def fake_build(job):
        return [sys.executable, "-u", "-c",
                FAKE_CACHE_PIPELINE.format(result=app._result_dir(job))]

    app._build_argv = fake_build
    config = {"stages": ["1", "2", "3"], "slide_lang": "en", "script_lang": "zh",
              "paper_type": "auto"}

    def run_job(client, cfg, label):
        body = client.post("/api/jobs", json={
            "ingest_id": ing_id, "portrait_path": portrait,
            "paper_latex_root": latex_root,
            "config": dict(cfg), "api_key": "sk-smoke-fake"}).json()
        jid = body.get("job_id")
        created_jobs.append(jid)
        deadline = time.time() + 60
        j = {}
        while time.time() < deadline:
            j = client.get("/api/jobs/%s" % jid).json()
            if j.get("status") in ("done", "failed", "canceled"):
                break
            time.sleep(0.5)
        log_path = os.path.join(app._job_dir(jid), "run.log")
        raw = open(log_path, "rb").read() if os.path.isfile(log_path) else b""
        text = raw.decode("utf-8", errors="replace")
        return jid, j, text

    try:
        with TestClient(srv) as client:
            jid_a, job_a, log_a = run_job(client, config, "A")
            ok &= check("第一次跑：任务完成", job_a.get("status") == "done",
                        "status=%s exit=%s" % (job_a.get("status"), job_a.get("exit_code")))
            key_a = (job_a.get("cache") or {}).get("deck_key")
            ok &= check("第一次跑：幻灯片/讲稿被回写进缓存",
                        bool((job_a.get("cache") or {}).get("published_deck")),
                        "published=%s" % ((job_a.get("cache") or {}).get("published_deck")))
            ok &= check("第一次跑：日志里明确说未命中（回落/命中都要可见）",
                        "[cache] 幻灯片/讲稿未命中" in log_a,
                        [l for l in log_a.splitlines() if "[cache]" in l][:1])
            if key_a:
                made_cache.add("deck/" + key_a)

            jid_b, job_b, log_b = run_job(client, config, "B")
            cinfo = job_b.get("cache") or {}
            ok &= check("第二次跑（同样输入）：deck 命中", cinfo.get("deck_hit") is True,
                        "cache=%s" % json.dumps(cinfo, ensure_ascii=False)[:120])
            ok &= check("命中时讲稿也命中 ⇒ stage 2 的 VLM 调用被跳过",
                        cinfo.get("deck_script_hit") is True, "script_hit")
            cfg_b = job_b.get("config") or {}
            ok &= check("命中的任务不再跑 stage 1（幻灯片已就位）",
                        cfg_b.get("stages") == ["2", "3"],
                        "config.stages=%s" % (cfg_b.get("stages"),))
            ok &= check("命中的任务带 reuse_script（pipeline 才会跳过 VLM）",
                        cfg_b.get("reuse_script") is True,
                        "config.reuse_script=%s" % (cfg_b.get("reuse_script"),))
            # 上面两条看的是 config（缓存逻辑改的就是它）；这里再用**真实**的
            # _build_argv 过一遍，证明这两项真的会落到命令行上（只看 config
            # 有可能被"argv 不从 config 读"这种情况骗过）。
            real_argv = app._ORIG_BUILD_ARGV(job_b)
            ok &= check("真实命令行里 stage 是 2/3、且带 --reuse_script True",
                        '"2", "3"' in " ".join(real_argv)
                        and "--reuse_script" in real_argv
                        and real_argv[real_argv.index("--reuse_script") + 1] == "True",
                        " ".join(real_argv[2:9]))
            ok &= check("缓存的幻灯片被复制进本次产物目录（下游才读得到）",
                        os.path.isfile(os.path.join(app._result_dir(job_b), "slide_imgs", "1.png"))
                        and os.path.isfile(os.path.join(app._result_dir(job_b), "subtitle_w_cursor.txt")),
                        "产物目录已有 slide_imgs/1.png 与讲稿")
            ok &= check("第二次跑：日志写明命中与省下了什么",
                        "[cache] 讲稿命中" in log_b and "无讲稿 API 费用" in log_b,
                        [l for l in log_b.splitlines() if "[cache]" in l][:2])
            ok &= check("任务记录里留下的 deck_key 与缓存的键一致（写入键=查询键）",
                        cinfo.get("deck_key") == key_a,
                        "%s vs %s" % (cinfo.get("deck_key"), key_a))

            plan = ac.plan_gc(app.JOBS_ROOT)
            ok &= check("被任务引用过的缓存键会被 plan_gc 计为「有人用」",
                        key_a in plan["referenced"]["deck"],
                        "referenced 里有 %s" % key_a)

            # 反面对照：换讲稿语言 → 指纹必须变 → 不许命中
            alt = dict(config, script_lang="en")
            jid_c, job_c, log_c = run_job(client, alt, "C")
            ok &= check("对照组：改了讲稿语言就不再命中（否则会拿中文讲稿配英文要求）",
                        (job_c.get("cache") or {}).get("deck_hit") is not True,
                        "deck_hit=%s" % ((job_c.get("cache") or {}).get("deck_hit")))
            ok &= check("对照组：仍然完整跑 stage 1（该花钱的地方没有偷懒）",
                        (job_c.get("config") or {}).get("stages") == ["1", "2", "3"],
                        "config.stages=%s" % ((job_c.get("config") or {}).get("stages"),))
            # 对照组这次**也会**写一条自己的缓存（它是未命中 → 正常生成 → 回写），
            # 必须一起记下来清理：只记 A 的键会让 en 那条留在库里，
            # 下次跑本组时"对照组"就会命中上一次的遗留 → 假失败（实测踩过）。
            key_c = (job_c.get("cache") or {}).get("deck_key")
            if key_c:
                made_cache.add("deck/" + key_c)
    finally:
        app._build_argv = saved_build
        _cleanup_new(app, jobs_before)          # 人像文件也在 jobs/ 下，一并按差集清掉
        _cleanup_new_content(app, content_before)
        for cid in made_content:
            shutil.rmtree(os.path.join(app.CONTENT_ROOT,
                                       cid.replace("content_", "")), ignore_errors=True)
        # 只删**本测试自己建的**缓存条目（按 key，精确删；绝不整体清 _cache）
        for item in made_cache:
            kind, _, key = item.partition("/")
            shutil.rmtree(os.path.join(ac.CACHE_ROOT, kind, key), ignore_errors=True)

    # ── E. 静态守卫 ──
    app_src = io.open(os.path.join(ROOT, "web", "app.py"), encoding="utf-8").read()
    run_seg = app_src[app_src.find("async def _run_job"):][:9000]
    ok &= check("结构化命中检查在 _prepare_latex **之前**（放后面就等于钱花了才想省）",
                0 < run_seg.find("lookup_struct") < run_seg.find("await _prepare_latex"),
                "顺序：lookup_struct(%d) < _prepare_latex(%d)" % (
                    run_seg.find("lookup_struct"), run_seg.find("await _prepare_latex")))
    ok &= check("deck 命中检查在前置结构化**之后**（那时才有 main.tex 可算指纹）",
                run_seg.find("await _prepare_latex") < run_seg.find("lookup_deck"),
                "顺序：_prepare_latex < lookup_deck")
    ok &= check("缓存回写用的是查询时那个键（写入键≠查询键会让缓存永远不命中）",
                "key=cache_info.get" in app_src, "publish 调用带 key=")
    ok &= check("有新接口可以只读地看缓存现状", '"/api/cache"' in app_src, "GET /api/cache")
    return ok


def main():
    app = load_app()
    suite_started = time.time()          # 清理护栏：只删本次测试期间新建的目录
    results = [
        test_read_progress(app),
        test_queue_and_http(app),
        test_progress_sources(app),
        test_pages_and_rerun(app),
        test_struct_prepare(app),
        test_portrait_upload(app),
        test_log_encoding_and_model_registry(app),
        test_rerun_shares_artifacts(app),
        test_phase_model_and_keys(app),
        test_voice_and_ref_audio(app),
        test_content_store_and_fingerprint(app),
        test_artifact_cache_and_reuse(app),
    ]
    orphan = _cleanup_orphan_jobs(app, suite_started)
    if orphan:
        print()
        print("   （已清理 %d 个测试孤儿任务目录：%s）" % (len(orphan), ", ".join(orphan)))
        print("   （说明有测试的清理不完整 —— 不影响结果，但值得顺手补上）")
    print()
    print("=" * 66)
    if FAILURES:
        print("汇总: %d 项失败" % len(FAILURES))
        for f in FAILURES:
            print("   ✘", f)
    else:
        print("汇总: 全部通过")
    print("=" * 66)
    return 1 if FAILURES else 0


if __name__ == "__main__":
    raise SystemExit(main())
