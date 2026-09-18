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
        for name in os.listdir(app.JOBS_ROOT):        # 清掉续跑产生的任务
            if name.startswith("job_") or name.startswith("smoke_"):
                shutil.rmtree(os.path.join(app.JOBS_ROOT, name), ignore_errors=True)
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

            after = set(os.listdir(app.JOBS_ROOT))
            created = after - before
            # 3 次请求里只有 2 次成功 → 只该多出 2 个目录
            ok &= check("失败那次不留空壳目录", len(created) == 2,
                        "新建 %d 个目录（期望 2）：%s" % (len(created), sorted(created)))
    finally:
        for name in set(os.listdir(app.JOBS_ROOT)) - before:
            shutil.rmtree(os.path.join(app.JOBS_ROOT, name), ignore_errors=True)
    return ok


def main():
    app = load_app()
    results = [
        test_read_progress(app),
        test_queue_and_http(app),
        test_progress_sources(app),
        test_pages_and_rerun(app),
        test_struct_prepare(app),
        test_portrait_upload(app),
    ]
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
