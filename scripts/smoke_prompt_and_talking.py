"""讲稿提示词编辑 + 可选数字人的专项冒烟测试。

不调用模型、ffmpeg 或真实流水线，只验证 Web 配置到命令行的完整参数链，
以及无效提示词不会留下任务目录。
"""
import asyncio
import importlib.util
import io
import os
import shutil
import sys
import uuid


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB_APP = os.path.join(ROOT, "web", "app.py")


def load_app():
    spec = importlib.util.spec_from_file_location("p2v_web_feature_smoke", WEB_APP)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def check(label, condition, detail=""):
    if not condition:
        raise AssertionError("{}：{}".format(label, detail or "未通过"))
    print("PASS {}{}".format(label, " · " + detail if detail else ""))


async def main_async():
    web = load_app()
    web._QUEUE = asyncio.Queue()
    suffix = uuid.uuid4().hex[:8]
    ingest_id = "smoke_feature_ingest_" + suffix
    ingest_dir = os.path.join(web.JOBS_ROOT, ingest_id)
    os.makedirs(os.path.join(ingest_dir, "ingest"), exist_ok=True)
    io.open(os.path.join(ingest_dir, "ingest", "main.tex"), "w",
            encoding="utf-8").write("% feature smoke\n")

    created = []
    try:
        options = await web.api_options()
        profiles = options.get("script_prompt_profiles") or []
        check("下发五种独立讲稿内容模板",
              [p["value"] for p in profiles]
              == ["paper", "textbook", "story", "fiction_comic", "custom"])
        check("每种模板都有中英文完整提示词",
              all("###" in p["prompts"]["zh"] and "|" in p["prompts"]["en"]
                  for p in profiles))
        check("各预制提示词不是论文模板追加覆盖指令",
              len({p["prompts"]["zh"] for p in profiles}) == len(profiles)
              and all("内容类型覆盖指令" not in p["prompts"]["zh"] for p in profiles))

        custom_prompt = (
            "你是一位课程教师，请用通俗语言讲解每页内容。\n"
            "每句话格式：讲稿 | \"幻灯片原文\"。\n"
            "不同页面使用 ### 分隔。"
        )
        payload = {
            "ingest_id": ingest_id,
            # 故意不传 portrait_path，同时把开关设为 True：后端必须自动降级。
            "config": {
                "stages": ["1", "2", "3"],
                "slide_lang": "en",
                "script_lang": "zh",
                "paper_type": "auto",
                "content_profile": "textbook",
                "script_prompt": custom_prompt,
                "generate_talking": True,
                "voice": {"kind": "default"},
            },
        }
        response = await web.api_create_job(payload)
        job_id = response["job_id"]
        created.append(job_id)
        job = web._load_job(job_id)
        cfg = job["config"]

        check("无人像时自动关闭数字人",
              response["generate_talking"] is False
              and cfg["generate_talking"] is False
              and job.get("ref_img") is None,
              response.get("talking_note", ""))
        prompt_path = cfg.get("script_prompt_path")
        saved_prompt = io.open(prompt_path, encoding="utf-8").read().strip()
        check("编辑后的提示词按任务保存", saved_prompt == custom_prompt,
              os.path.basename(prompt_path or ""))
        check("任务只记录提示词路径与哈希，不在 job.json 重复全文",
              "script_prompt" not in cfg and bool(cfg.get("script_prompt_sha1")))

        argv = web._build_argv(job)
        check("命令行关闭数字人但仍保留 stage 3",
              argv[argv.index("--generate_talking") + 1] == "False"
              and '"3"' in argv[argv.index("--stage") + 1])
        check("命令行传提示词文件且不传空人像",
              argv[argv.index("--script_prompt_file") + 1] == prompt_path
              and "--ref_img" not in argv)

        progress_job = dict(job, status="running", phase_key="merge", phase="合成视频")
        progress = web.read_progress(progress_job)
        check("关闭数字人后进度阶段减为 8 个",
              progress["phase_count"] == 8 and progress["phase_key"] == "merge",
              "phase_count={}".format(progress["phase_count"]))

        facts = {
            "main_tex_sha1": "main", "source_sha1": "source",
            "slide_lang": "en", "script_lang": "zh", "paper_type": "auto",
            "llm_model": "deepseek-flash", "vlm_model": "deepseek-flash",
            "talking_profile": "musetalk-local", "if_tree_search": False,
            "figures": "v1", "script_prompt_sha1": "prompt-a",
        }
        key_a = web.artifact_cache.deck_key(facts)
        key_b = web.artifact_cache.deck_key(dict(facts, script_prompt_sha1="prompt-b"))
        check("修改提示词会使讲稿缓存失效", key_a != key_b,
              "{} != {}".format(key_a, key_b))

        before = set(os.listdir(web.JOBS_ROOT))
        try:
            await web.api_create_job({
                "ingest_id": ingest_id,
                "config": dict(payload["config"], script_prompt="没有机器格式约束"),
            })
            raise AssertionError("无效提示词未被拒绝")
        except web.HTTPException as exc:
            check("无效提示词返回 400", exc.status_code == 400, str(exc.detail))
        after = set(os.listdir(web.JOBS_ROOT))
        check("无效提示词不留下孤儿任务目录", before == after)

        tsx = io.open(os.path.join(ROOT, "web", "frontend", "src", "App.tsx"),
                      encoding="utf-8").read()
        check("前端包含内容类型、提示词编辑器和数字人开关",
              all(token in tsx for token in (
                  "script_prompt_profiles", "prompt-editor", "generate_talking",
                  "未上传人像，本次将自动跳过数字人")))
        pipeline = io.open(os.path.join(ROOT, "Paper2Video-main", "src", "pipeline.py"),
                           encoding="utf-8").read()
        check("主流水线关闭数字人时改走音频直合片",
              "if args.generate_talking:" in pipeline
              and "merge_slides_and_audio(" in pipeline)
    finally:
        for job_id in created:
            web._JOB_KEYS.pop(job_id, None)
            job_dir = os.path.join(web.JOBS_ROOT, job_id)
            if os.path.isdir(job_dir):
                shutil.rmtree(job_dir)
        if os.path.isdir(ingest_dir):
            shutil.rmtree(ingest_dir)


def main():
    asyncio.run(main_async())
    print("\n专项冒烟测试全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
