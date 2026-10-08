"""操作流程回归：临时目录内测试续跑 API，不启动 worker、不调用模型、不碰真实产物。"""
import asyncio
import importlib.util
import json
import os
from pathlib import Path
import tempfile

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("flow_smoke_app", ROOT / "web" / "app.py")
web = importlib.util.module_from_spec(spec)
spec.loader.exec_module(web)


def main():
    checks = 0

    def check(condition, description):
        nonlocal checks
        assert condition, description
        checks += 1
        print("PASS", description)

    with tempfile.TemporaryDirectory(prefix=".flow-smoke-", dir=ROOT) as work:
        # 清理只针对本次创建的临时目录；验证最终绝对路径仍在工作区内。
        assert os.path.commonpath([str(ROOT), os.path.realpath(work)]) == str(ROOT)
        web.JOBS_ROOT = work
        web.CONTENT_ROOT = os.path.join(work, "_content")
        web.mimo_voice.CACHE_DIR = os.path.join(work, "_voices")
        web.artifact_cache.CACHE_ROOT = os.path.join(work, "_cache")
        web._QUEUE = asyncio.Queue()
        result = Path(work) / "source" / "result"
        slides = result / "slide_imgs"
        slides.mkdir(parents=True)
        (slides / "1.png").write_bytes(b"isolated slide fixture")
        script = result / "subtitle_w_cursor.txt"
        script.write_text('讲稿 | "Title"', encoding="utf-8")
        job = {"id": "source", "status": "done", "source_name": "测试论文.pdf",
               "config": {"generate_talking": False, "slide_lang": "zh", "script_lang": "zh"}}
        web._save_job(job)
        client = TestClient(web.app, client=('127.0.0.1',50000))
        async def first_frame():
            frames = web._sse_frames("source", lambda: False, 15)
            try:
                return await anext(frames)
            finally:
                await frames.aclose()
        frame = asyncio.run(first_frame())
        event = json.loads(frame.split("data: ", 1)[1])
        check(event["config"]["slide_lang"] == "zh" and not event["config"]["generate_talking"],
              "实时流携带任务自身配置，详情不借用新建草稿")

        plan = client.get("/api/jobs/source/rerun-plan").json()
        check(plan["can_rerun"] and plan["can_reuse_script"], "完整讲稿可以预检并复用")
        check("合成视频" in plan["overwrite"], "预检说明原产物覆盖范围")
        script.write_text('截断 | "Title', encoding="utf-8")
        stale = client.post("/api/jobs/source/rerun", json={"plan_token": plan["token"], "reuse_script": True})
        check(stale.status_code == 409 and web._QUEUE.empty(), "确认后产物变化被拦截，不创建任务：{} {}".format(stale.status_code,stale.text))
        invalid = client.get("/api/jobs/source/rerun-plan").json()
        check(not invalid["can_reuse_script"] and invalid["can_rerun"], "损坏讲稿不能承诺免费复用")
        refused = client.post("/api/jobs/source/rerun", json={"plan_token": invalid["token"], "reuse_script": True})
        check(refused.status_code == 409 and web._QUEUE.empty(), "损坏讲稿不会静默改为调用模型")
        paid = client.post("/api/jobs/source/rerun", json={"plan_token": invalid["token"], "reuse_script": False})
        check(paid.status_code == 200, "明确选择重新生成后可以入队")
        paid_job = web._load_job(paid.json()["job_id"])
        check(not paid_job["config"]["reuse_script"] and not paid_job["config"]["require_reuse_script"], "重新生成的配置不伪装成免费复用")
        conflict = client.get("/api/jobs/source/rerun-plan").json()
        check(not conflict["can_rerun"], "共享产物目录已有排队任务时禁止再次覆盖")
        paid_job["status"] = "canceled"
        web._save_job(paid_job)
        script.write_text('讲稿 | "Title"', encoding="utf-8")
        plan = client.get("/api/jobs/source/rerun-plan").json()
        resumed = client.post("/api/jobs/source/rerun", json={"plan_token": plan["token"], "reuse_script": True})
        check(resumed.status_code == 200, "合法免费复用可以创建任务")
        resumed_job = web._load_job(resumed.json()["job_id"])
        argv = web._build_argv(resumed_job)
        check("--require_reuse_script" in argv and resumed_job["config"]["require_reuse_script"], "执行时严格复用，缓存失效也不自动付费")
        resumed_job["status"] = "canceled"
        web._save_job(resumed_job)
        (slides / "1.png").unlink()
        check(not client.get("/api/jobs/source/rerun-plan").json()["can_rerun"], "未生成幻灯片的失败任务需要新建")
        check(client.get("/api/jobs/missing/rerun-plan").status_code == 404, "缺失任务有明确错误")

        voice = web.mimo_voice
        os.makedirs(voice.CACHE_DIR, exist_ok=True)
        item = next(v for v in voice.PRESET_VOICES if v["lang"] == "zh")
        key = voice.cache_key("preset", item["id"], None, "zh")
        wav, txt, meta = voice._paths(key)
        Path(wav).write_bytes(b"audio fixture")
        Path(txt).write_text("试听文本", encoding="utf-8")
        Path(meta).write_text(json.dumps({"key": key, "kind": "preset", "lang": "zh", "duration_s": 5}), encoding="utf-8")
        saved = next(v for v in voice.voices_for("zh")["presets"] if v["spec"]["voice"] == item["id"])
        check(saved["sample_url"].endswith(key + "/sample"), "已保存的预置音色直接返回试听链接")
        check(saved["ref_text"] == "试听文本", "缓存试听带上正确的参考文本")
    print("全部通过：{} 项；未启动真实流水线".format(checks))


if __name__ == "__main__":
    main()
