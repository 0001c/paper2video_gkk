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
import sys, time
def p(s):
    print(s); sys.stdout.flush()
p("### fake pipeline (smoke test)")
p("start stage 1")
for i in range({pages}):
    p("[speech_gen] 第 {{}} 页合成完成".format(i))
    time.sleep({sleep})
for i in range({pages}):
    p("[talking_gen] 第 {{}} 页完成".format(i))
    time.sleep({sleep})
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

    # 造一个合法的 ingest 目录与人像文件
    ingest_id = "smoke_ing_" + uuid.uuid4().hex[:6]
    ing_dir = os.path.join(app.JOBS_ROOT, ingest_id)
    os.makedirs(os.path.join(ing_dir, "ingest"), exist_ok=True)
    io.open(os.path.join(ing_dir, "ingest", "paper_text.md"), "w",
            encoding="utf-8").write("# 冒烟测试\n正文\n")
    portrait = os.path.join(ing_dir, "portrait.png")
    io.open(portrait, "wb").write(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)

    created = []
    try:
        with TestClient(srv) as client:
            # ── 任务 A：跑完（走 done 路径）──
            r = client.post("/api/jobs", json={
                "ingest_id": ingest_id, "portrait_path": portrait,
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


def main():
    app = load_app()
    results = [test_read_progress(app), test_queue_and_http(app)]
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
