"""Paper2Video Web 服务（P0）。

设计上必须守住的四条（都写在方案文档里，这里集中复述一遍，避免改代码时走偏）：

1. **默认只监听 127.0.0.1**。局域网共享必须显式指定可信子网，
   该子网内设备可操作任务和读取产物；不能作为已鉴权的公网服务。
2. **单 worker 串行队列**。单卡 16GB，两个任务同时跑必然 OOM。
   多组合是"排队"而不是"并行"。
3. **流水线走子进程，且不经 shell**。显存释放靠进程退出保证；
   `create_subprocess_exec(argv...)` 而不是 `shell=True`，
   因为本机 MSYS 的 `cd` 遇到含中文的路径会失败，且 shell 会引入转义问题。
4. **开关可以暴露，但必须能被指纹看见**。`--if_tree_search`（幻灯片布局微调）
   曾经写死 False，理由是"默认值 True 会 NameError"—— 那个崩溃点来自上游
   `pipeline.py` 里一条引用被注释掉 import 的 else 分支，而我们这边已合并成单分支，
   它早就不存在了。现在它是**可配置项、默认关**（成本确定、收益未实测）。
   ⚠️ 一旦某个开关会改变产物，它就必须同时进入 `artifact_cache` 的指纹字段表 ——
   否则产物缓存会拿别的设置下产出的结果当成自己的（静默错配）。
   反之不影响产物的开关**不要**加进字段表，否则白花一次钱。

启动：
    python web/app.py            # 默认 127.0.0.1:8848
"""
import asyncio
import hashlib
import inspect
import ipaddress
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
from pathlib import Path
from pydantic import BaseModel, ConfigDict, Field

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
import pv_env                                             # noqa: E402
import mimo_voice                                         # noqa: E402
import artifact_cache                                     # noqa: E402
import paper_figures                                      # noqa: E402
import course                                             # noqa: E402
import cursor_styles                                      # noqa: E402
from script_validation import validate_script_output      # noqa: E402

os.makedirs(JOBS_ROOT, exist_ok=True)
app = FastAPI(title="Paper2Video Web", version="0.1.0")
app.state.lan_networks = ()


def _lan_network(value):
    network = ipaddress.ip_network(value, strict=False)
    private_ranges = ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
    if network.version != 4 or not any(network.subnet_of(ipaddress.ip_network(block)) for block in private_ranges):
        raise ValueError("局域网子网必须位于 IPv4 私有地址范围，例如 192.168.31.0/24")
    return network


def _trusted_address(host):
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return address.is_loopback or any(address in network for network in app.state.lan_networks)


@app.middleware("http")
async def trusted_lan_access(request, call_next):
    if app.state.lan_networks:
        if not request.client or not _trusted_address(request.client.host):
            return JSONResponse(status_code=403, content={"detail": "访问设备不在允许的局域网子网内"})
        # 仅使用 IP 地址或 localhost，阻止恶意域名重绑定及跨站表单写入。
        if request.url.hostname != "localhost" and not _trusted_address(request.url.hostname or ""):
            return JSONResponse(status_code=403, content={"detail": "请使用本机或局域网 IP 地址访问"})
        if request.method in ("POST", "PUT", "PATCH", "DELETE"):
            try:
                _local_write(request)
            except HTTPException as exc:
                return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})
    return await call_next(request)


@app.middleware('http')
async def review_body_limit(request, call_next):
    if request.method in ('PUT', 'POST') and (request.url.path.endswith('/review') or request.url.path.endswith('/review/run') or request.url.path == '/api/course/plan'):
        try:
            _local_write(request)
            length = int(request.headers.get('content-length') or 0)
        except HTTPException as exc:
            return JSONResponse(status_code=exc.status_code, content={'detail': exc.detail})
        except ValueError:
            return JSONResponse(status_code=400, content={'detail': 'Content-Length 无效'})
        if length > 512000 or len(await request.body()) > 512000:
            return JSONResponse(status_code=413, content={'detail': '审核请求体最多 512000 字节'})
    return await call_next(request)


def _local_write(request):
    # 默认本机；局域网模式仅信任启动时明确允许的子网，同源校验仍保留。
    if not request.client or not _trusted_address(request.client.host):
        raise HTTPException(503, detail="写入仅支持本机或明确允许的局域网子网")
    origin = request.headers.get("origin")
    if origin and origin.rstrip("/") != str(request.base_url).rstrip("/"):
        raise HTTPException(403, detail="拒绝跨站审核写入")


def _course_source(work):
    text_file = Path(work, "ingest", "paper_text.md")
    if text_file.is_file():
        return text_file.read_text(encoding="utf-8")
    root = (_content_meta(work) or {}).get("latex_root")
    if root and Path(root, "main.tex").is_file():
        try:
            return course.latex_source(root)
        except (ValueError, OSError, UnicodeError) as exc:
            raise HTTPException(422, detail=str(exc))
    raise HTTPException(422, detail="section_ids：没有可读取的正文，请重新提取或上传选定小节")


class CoursePlanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ingest_id: str = Field(max_length=100)
    settings: dict
    slide_count: int = Field(ge=4, le=40)
    script_lang: str = Field(pattern="^(zh|en)$")


class ReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: str = Field(min_length=64, max_length=64)
    pages: list[str] = Field(min_length=1, max_length=200)


class ReviewRunRequest(ReviewRequest):
    action: str = Field(pattern="^(render|regenerate)$")
    page_ids: list[int] = Field(default_factory=list, max_length=40)
    api_key: str | None = Field(default=None, max_length=2000)
    api_base: str | None = Field(default=None, max_length=2000)
    refresh_prompt: bool = False
    text_model: str | None = Field(default=None, min_length=1, max_length=200)
    vision_model: str | None = Field(default=None, min_length=1, max_length=200)
    script_concurrency: int | None = Field(default=None, ge=1, le=8)
    cursor_style: str | None = Field(default=None, max_length=50)
    cursor_scale: float | None = None


class PrepareRetryRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    api_key: str | None = Field(default=None, max_length=2000)
    api_base: str | None = Field(default=None, max_length=2000)


@app.post('/api/jobs/{job_id}/prepare-retry')
async def api_prepare_retry(job_id: str, payload: PrepareRetryRequest, request: Request):
    _local_write(request)
    async with _JOB_LOCK:
        src = _load_job(job_id)
        result = _result_dir(src)
        if src.get('status') not in ('failed', 'canceled') or not src.get('config', {}).get('review_before_render'):
            raise HTTPException(409, detail='仅支持失败或取消的准备任务')
        if any(j.get('status') in ('queued', 'running') and os.path.realpath(_result_dir(j)) == os.path.realpath(result) for j in _list_jobs()):
            raise HTTPException(409, detail='共享产物正在使用中，请等待结束')
        if not os.path.isfile(os.path.join(src.get('paper_latex_root') or '', 'main.tex')) and not os.path.isfile(src.get('source_path') or ''):
            raise HTTPException(409, detail='原素材不可用，请重新上传')
        config = dict(src['config'])
        _normalize_models(config)
        if config.get('regenerate_pages') is not None and not config.get('script_generation_id'):
            config['script_generation_id'] = uuid.uuid4().hex  # 旧重写任务首次恢复后也具备稳定缓存标识。
        if payload.api_base:
            config['model_api_base'] = _model_base(payload.api_base)
        config.update(result_dir=result, reuse_script=False, require_reuse_script=False,
                      review_before_render=True, stages=['2'] if os.path.isfile(os.path.join(result, 'slides_rendered.pdf')) else ['1', '2'])
        # 局部重写失败后继续重写原来选中的页，保留其它已审核的讲稿。
        new_id = 'job_' + time.strftime('%m%d_%H%M%S') + '_' + uuid.uuid4().hex[:4]
        new_dir = _job_dir(new_id)
        os.makedirs(new_dir, exist_ok=True)
        ingest = os.path.join(_job_dir(job_id), 'ingest')
        if os.path.isdir(ingest):
            shutil.copytree(ingest, os.path.join(new_dir, 'ingest'), dirs_exist_ok=True)
        _remember_keys(new_id, _JOB_KEYS.get(job_id))
        _remember_keys(new_id, payload.model_dump(exclude_none=True))
        job = {k: src.get(k) for k in ('ref_img', 'paper_latex_root', 'needs_struct', 'source_path', 'source_name', 'source_digest', 'ingest_id')}
        job.update(id=new_id, created_at=time.strftime('%Y-%m-%d %H:%M:%S'), status='queued', phase='排队中',
                   phase_key='queued', queue_ahead=_QUEUE.qsize(), config=config, resumed_from=job_id)
        _save_job(job)
        await _QUEUE.put(new_id)
        return {'ok': True, 'job_id': new_id}


@app.get("/api/course/source/{ingest_id}")
async def api_course_source(ingest_id: str):
    work = _upload_work(ingest_id)
    if not work or not os.path.isdir(work):
        raise HTTPException(404, detail="内容不存在")
    text = _course_source(work)
    return {"sections": course.source_sections(text), "chars": len(text)}


@app.post("/api/course/plan")
async def api_course_plan(payload: CoursePlanRequest, request: Request):
    _local_write(request)
    work = _upload_work(payload.ingest_id)
    if not work or not os.path.isdir(work):
        raise HTTPException(404, detail="ingest_id：内容不存在")
    try:
        plan, _ = course.build_plan(_course_source(work), payload.settings, payload.slide_count, payload.script_lang)
    except ValueError as exc:
        raise HTTPException(422, detail=str(exc))
    return course.public_plan(plan)


def _review_snapshot(job):
    result = Path(_result_dir(job))
    script = result / "subtitle_w_cursor.txt"
    if not script.is_file():
        raise HTTPException(409, detail="讲稿尚未生成，暂不能审核")
    pages = course.split_pages(script.read_text(encoding="utf-8"))
    slides = sorted((result / "slide_imgs").glob("*.png"), key=lambda p: int(p.stem))
    if len(pages) != len(slides) or not pages:
        raise HTTPException(409, detail="讲稿与课件页数不一致，请重新生成")
    plan = course.read_json((job.get("config") or {}).get("course_plan_path") or "")
    language = (job.get('config') or {}).get('script_lang', 'zh')
    audio_plan = plan or {'sections': [{'id': 'all', 'title': '讲解内容', 'minutes': len(pages) * 0.5}],
                          'script_lang': language, 'speech_rate': 220 if language == 'zh' else 140}
    report = course.audio_report(audio_plan, result)
    version = course.digest({"script": script.read_bytes().hex(), "slides": [course.digest(p.read_bytes()) for p in slides],
                             "plan": plan, "voice": (job.get("config") or {}).get("voice")})
    return {"version": version, "pages": pages, "plan": course.public_plan(plan) if plan else None,
            "timing": report, "status": job.get("status"), "editable": _rerun_plan(job)["can_rerun"]}


def _check_review(job, payload):
    snapshot = _review_snapshot(job)
    if not snapshot["editable"]:
        raise HTTPException(409, detail="任务或共享产物正在使用中，请等待结束")
    if payload.version != snapshot["version"]:
        raise HTTPException(409, detail="讲稿或课件已变化，请重新读取审核内容")
    if len(payload.pages) != len(snapshot["pages"]):
        raise HTTPException(422, detail="pages：必须提供与课件数量一致的全部讲稿页")
    if sum(len(p) for p in payload.pages) > 200000 or any(not p.strip() or len(p) > 20000 or len(course.split_pages(p)) != 1 for p in payload.pages):
        raise HTTPException(422, detail="pages：每页应为 1～20000 字符，不能含页分隔符，总量最多 200000 字符")
    text = "\n###\n".join(p.strip() for p in payload.pages)
    ok, why = validate_script_output(text, len(payload.pages))
    if not ok:
        raise HTTPException(422, detail="pages：" + why)
    if any(not course.spoken_text(p).strip() for p in payload.pages):
        raise HTTPException(422, detail="pages：每页至少需要一句讲稿 | 光标目标")
    return text


def _save_review(job, text):
    result = Path(_result_dir(job))
    script = result / "subtitle_w_cursor.txt"
    # 人工保存与模型重写共用原子替换和历史版本，避免中途取消损坏已审核讲稿。
    course.save_script(script, text)


@app.get("/api/jobs/{job_id}/review")
async def api_review(job_id: str):
    return _review_snapshot(_load_job(job_id))


@app.put("/api/jobs/{job_id}/review")
async def api_save_review(job_id: str, payload: ReviewRequest, request: Request):
    _local_write(request)
    async with _JOB_LOCK:
        job = _load_job(job_id)
        text = _check_review(job, payload)
        _save_review(job, text)
        return _review_snapshot(job)


@app.post("/api/jobs/{job_id}/review/run")
async def api_run_review(job_id: str, payload: ReviewRunRequest, request: Request):
    _local_write(request)
    async with _JOB_LOCK:
        job = _load_job(job_id)
        text = _check_review(job, payload)
        checked = dict(job.get('config') or {})
        if payload.script_concurrency is not None:
            checked['script_concurrency'] = payload.script_concurrency
        for field in ('text_model', 'vision_model'):
            if getattr(payload, field) is not None:
                checked[field] = getattr(payload, field)
        for field in ('cursor_style', 'cursor_scale'):
            if getattr(payload, field) is not None:
                checked[field] = getattr(payload, field)
        _normalize_models(checked)
        _model_base(payload.api_base)
        if payload.action == "regenerate":
            if not payload.page_ids or len(set(payload.page_ids)) != len(payload.page_ids) or any(i < 0 or i >= len(payload.pages) for i in payload.page_ids):
                raise HTTPException(422, detail="page_ids：请选择有效且不重复的零基页索引")
        elif payload.page_ids:
            raise HTTPException(422, detail="page_ids：配音合片时不要指定重写页")
        _save_review(job, text)
        plan = _rerun_plan(job)
        response = await api_rerun_job(job_id, {
            "plan_token": plan["token"], "reuse_script": payload.action == "render",
            "stages": ["2", "3"] if payload.action == "render" else ["2"],
            "review_pages": payload.page_ids if payload.action == "regenerate" else None,
            "api_key": payload.api_key, "api_base": payload.api_base,
            "refresh_script_prompt": payload.refresh_prompt and payload.action == 'regenerate',
            "text_model": payload.text_model, "vision_model": payload.vision_model,
            "script_concurrency": payload.script_concurrency,
            "cursor_style": payload.cursor_style, "cursor_scale": payload.cursor_scale})
        return response

# ──────────────────────────────────────────────────────────────
# 讲稿提示词模板：前端可编辑，但后端保留模板与安全边界
# ──────────────────────────────────────────────────────────────
SCRIPT_PROMPT_MAX_CHARS = 20000
SLIDE_PROMPT_MAX_CHARS = 30000
SLIDE_COUNT_MIN = 4
SLIDE_COUNT_MAX = 40
_PROMPT_DIR = os.path.join(P2V_SRC, "prompts")
_SCRIPT_PROMPT_FILES = {
    "paper": {
        "zh": os.path.join(_PROMPT_DIR, "slide_subtitle_cursor_prompt_zh.txt"),
        "en": os.path.join(_PROMPT_DIR, "slide_subtitle_cursor_prompt.txt"),
    },
    "textbook": {
        "zh": os.path.join(_PROMPT_DIR, "slide_subtitle_cursor_prompt_textbook_zh.txt"),
        "en": os.path.join(_PROMPT_DIR, "slide_subtitle_cursor_prompt_textbook.txt"),
    },
    "story": {
        "zh": os.path.join(_PROMPT_DIR, "slide_subtitle_cursor_prompt_story_zh.txt"),
        "en": os.path.join(_PROMPT_DIR, "slide_subtitle_cursor_prompt_story.txt"),
    },
    "fiction_comic": {
        "zh": os.path.join(_PROMPT_DIR, "slide_subtitle_cursor_prompt_fiction_comic_zh.txt"),
        "en": os.path.join(_PROMPT_DIR, "slide_subtitle_cursor_prompt_fiction_comic.txt"),
    },
    "custom": {
        "zh": os.path.join(_PROMPT_DIR, "slide_subtitle_cursor_prompt_general_zh.txt"),
        "en": os.path.join(_PROMPT_DIR, "slide_subtitle_cursor_prompt_general.txt"),
    },
}
_SCRIPT_PROMPT_PROFILES = {
    "paper": {
        "label": "学术论文讲解",
        "desc": "保留当前学术会议风格，突出研究问题、方法、实验与结论",
    },
    "textbook": {
        "label": "教材 / 课程讲解",
        "desc": "教师授课风格，按知识递进解释概念、例子、公式与阶段小结",
    },
    "story": {
        "label": "故事讲述",
        "desc": "围绕情节、人物动机与转折进行连贯叙述，保留故事感和悬念",
    },
    "fiction_comic": {
        "label": "小说 / 漫画解说",
        "desc": "结合场景、角色、对白与画面节奏进行生动解说，不虚构原作事实",
    },
    "custom": {
        "label": "通用 / 自定义",
        "desc": "使用中性的内容讲解口吻，并在下方提示词中自由调整角色与风格",
    },
}
_SCRIPT_PROMPT_ALIASES = {"ebook": "textbook", "course": "textbook"}
_SLIDE_PROMPT_FILES = {
    "paper": {
        "zh": os.path.join(_PROMPT_DIR, "slide_beamer_prompt_zh.txt"),
        "en": os.path.join(_PROMPT_DIR, "slide_beamer_prompt.txt"),
    },
    "textbook": {
        "zh": os.path.join(_PROMPT_DIR, "slide_beamer_prompt_textbook_zh.txt"),
        "en": os.path.join(_PROMPT_DIR, "slide_beamer_prompt_textbook.txt"),
    },
    "story": {
        "zh": os.path.join(_PROMPT_DIR, "slide_beamer_prompt_story_zh.txt"),
        "en": os.path.join(_PROMPT_DIR, "slide_beamer_prompt_story.txt"),
    },
    "fiction_comic": {
        "zh": os.path.join(_PROMPT_DIR, "slide_beamer_prompt_fiction_comic_zh.txt"),
        "en": os.path.join(_PROMPT_DIR, "slide_beamer_prompt_fiction_comic.txt"),
    },
    "custom": {
        "zh": os.path.join(_PROMPT_DIR, "slide_beamer_prompt_general_zh.txt"),
        "en": os.path.join(_PROMPT_DIR, "slide_beamer_prompt_general.txt"),
    },
}


def _script_prompt_text(profile, lang):
    """读取某内容类型独立的完整讲稿提示词。"""
    profile = _SCRIPT_PROMPT_ALIASES.get(profile, profile)
    profile = profile if profile in _SCRIPT_PROMPT_PROFILES else "custom"
    lang = lang if lang in ("zh", "en") else "en"
    with io.open(_SCRIPT_PROMPT_FILES[profile][lang], encoding="utf-8") as handle:
        return handle.read().strip()


def _slide_prompt_text(profile, lang):
    """读取与讲解内容类型匹配的完整幻灯片提示词。"""
    profile = _SCRIPT_PROMPT_ALIASES.get(profile, profile)
    profile = profile if profile in _SLIDE_PROMPT_FILES else "custom"
    lang = lang if lang in ("zh", "en") else "en"
    with io.open(_SLIDE_PROMPT_FILES[profile][lang], encoding="utf-8") as handle:
        return handle.read().strip()


def _normalize_script_prompt(config):
    """校验并补全讲稿提示词，返回 ``(profile, prompt)``。"""
    profile = str(config.get("content_profile") or "paper").strip().lower()
    profile = _SCRIPT_PROMPT_ALIASES.get(profile, profile)
    if profile not in _SCRIPT_PROMPT_PROFILES:
        raise HTTPException(status_code=400, detail="不支持的内容类型：{}".format(profile))
    lang = str(config.get("script_lang") or "en").strip().lower()
    prompt = config.get("script_prompt")
    if prompt is None or not str(prompt).strip():
        prompt = _script_prompt_text(profile, lang)
    elif not isinstance(prompt, str):
        raise HTTPException(status_code=400, detail="讲稿提示词必须是文本")
    prompt = prompt.strip()
    if len(prompt) > SCRIPT_PROMPT_MAX_CHARS:
        raise HTTPException(
            status_code=400,
            detail="讲稿提示词过长（最多 {} 字符）".format(SCRIPT_PROMPT_MAX_CHARS))
    # 下游的讲稿解析器依赖这两个结构标记。允许自由改角色、语气与长度，
    # 但不能把机器可读的页分隔和光标字段一起删掉，否则 VLM 会反复重试后失败。
    if "###" not in prompt or "|" not in prompt:
        raise HTTPException(
            status_code=400,
            detail="讲稿提示词必须保留页分隔符 ### 和‘讲稿 | 光标目标’输出格式")
    return profile, prompt


def _normalize_models(config):
    try:
        cursor_styles.normalize(config)
    except ValueError as exc:
        raise HTTPException(422, detail=str(exc))
    for field, env in (('text_model', 'PAPER2VIDEO_LLM_MODEL'), ('vision_model', 'PAPER2VIDEO_VLM_MODEL')):
        name = config.get(field)
        if name is None:
            name = os.environ.get(env, 'deepseek-flash')
        if not isinstance(name, str) or not name.strip() or len(name) > 200 or any(ord(c) < 32 for c in name):
            raise HTTPException(422, detail=field + '：模型 ID 必须为 1～200 字符且不能包含控制字符')
        config[field] = name.strip()
    value = config.get('script_concurrency', 3)
    if type(value) is not int or not 1 <= value <= 8:
        raise HTTPException(422, detail='script_concurrency：必须为 1～8 的整数')
    config['script_concurrency'] = value


def _normalize_slide_prompt(config, profile):
    """校验幻灯片提示词与目标页数，返回 ``(prompt, count)``。"""
    lang = str(config.get("slide_lang") or "en").strip().lower()
    prompt = config.get("slide_prompt")
    if prompt is None or not str(prompt).strip():
        prompt = _slide_prompt_text(profile, lang)
    elif not isinstance(prompt, str):
        raise HTTPException(status_code=400, detail="幻灯片提示词必须是文本")
    prompt = prompt.strip()
    if len(prompt) > SLIDE_PROMPT_MAX_CHARS:
        raise HTTPException(
            status_code=400,
            detail="幻灯片提示词过长（最多 {} 字符）".format(SLIDE_PROMPT_MAX_CHARS))
    if "\\documentclass" not in prompt or "frame" not in prompt:
        raise HTTPException(
            status_code=400,
            detail="幻灯片提示词必须保留 LaTeX Beamer 的 documentclass 与 frame 输出要求")
    try:
        count = int(config.get("slide_count", 12))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="幻灯片页数必须是整数")
    if count < SLIDE_COUNT_MIN or count > SLIDE_COUNT_MAX:
        raise HTTPException(
            status_code=400,
            detail="幻灯片页数必须在 {}～{} 页之间".format(
                SLIDE_COUNT_MIN, SLIDE_COUNT_MAX))
    return prompt, count

# ──────────────────────────────────────────────────────────────
# 接口凭据：**按任务**存进程内存，不落盘
# ──────────────────────────────────────────────────────────────
# 为什么不落盘：本地/内网服务，key 写进文件就多一份泄漏面。
# 需要长期持久化的场景由使用者自己在仓库根的 .env 里配（pv_env 会读）。
#
# ⚠️ 为什么是 dict[job_id] 而不是"一份会话 key"（2026-09-18 修）：
#   原先 `_SESSION_KEYS` 是**模块级单例**，`/api/key/verify` 与建任务都往里写，
#   行为等价于"**最后输入的人覆盖所有人**"。单人本机完全无感，但迁到学校服务器
#   多人使用时，A 的 key 会被 B 的任务拿去用（B 甚至不知道自己花了 A 的钱）；
#   而一旦把 key 存进浏览器本地，这个覆盖**更难被发现**（每个人的界面都显示着
#   自己填的 key，看起来一切正常，实际执行用的是别人的）。
#   改成按 job_id 挂：任务之间互不可见，新建任务的 key 不会影响已在跑的任务。
_JOB_KEYS = {}


def _remember_keys(job_id, payload):
    """记下这个任务自己的凭据（缺项不覆盖已有值）。"""
    entry = _JOB_KEYS.setdefault(job_id, {})
    for field in ("api_key", "api_base"):
        value = (payload or {}).get(field)
        if value and str(value).strip():
            entry[field] = str(value).strip()


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


# ──────────────────────────────────────────────────────────────
# 上传内容库（内容寻址）
# ──────────────────────────────────────────────────────────────
# 为什么不再用随机 id（2026-09-19 改）：
#   原先三个上传端点都用 `uuid4().hex[:8]` 当目录名 ⇒ 同一份 PDF 上传三次就在
#   jobs/ 下留三份完全相同的副本（实测：一份 1.5 MB 的 PDF 有 3 个副本、同一张
#   人像 2 个，重复占用 3.8 MB）。比磁盘更贵的是**重复的解析与重复花钱的步骤**。
#
# 现在按**内容指纹**寻址：同一份文件必然落到同一个目录，于是
#   · 不重复落盘；· 提取结果（paper_text.md）直接复用；
#   · 将来把指纹接进 main.tex / 幻灯片缓存，就能省掉一次结构化调用。
#
# ⚠️ 三条基本要求（详见 docs/内容寻址与去重方案.md）
#   1. **不可变**：入库后永不修改，存成 `<sha1[:12]><扩展名>`，原文件名只记在 meta；
#   2. **只对原始上传文件取指纹**：派生结果（正文 / main.tex）不参与寻址，它们的
#      缓存键必须额外带处理版本号（见 paper_ingest.EXTRACTOR_VERSION）；
#   3. **删除要有引用意识**：一个内容目录可能被多个任务引用，不做"随任务删"。
CONTENT_ROOT = os.path.join(JOBS_ROOT, "_content")
_CONTENT_DIGEST_RE = re.compile(r"^[0-9a-f]{8,40}$")


def _content_dir(digest):
    """内容库里某份内容的目录。"""
    return os.path.join(CONTENT_ROOT, digest)


def _upload_work(upload_id):
    """把「上传 id」解析成工作目录 —— **唯一入口**。

    读取侧（建任务、预览、后续步骤）一律走这里，不许自己拼路径：命名规则这次从
    随机 id 改成了内容指纹，若还有地方按老规则拼，症状会是"文件明明在、却报找不到"，
    而且只在部分入口出现 —— 这类漂移必须靠收敛到一个函数来避免。

    两种格式并存，**都必须认**（浏览器里、老任务的记录里都还存着旧 id）：
      · ``content_<sha1[:12]>``  → ``jobs/_content/<sha1[:12]>``（新，内容寻址）
      · 任何**单个路径段**的名字   → ``jobs/<名字>``（旧随机 id，以及手工建的目录）
    解析不出来返回 ``None``，由调用方报 400。

    ⚠️ 校验的是「只有一个路径段」而不是「前缀在白名单里」：
    前缀白名单挡不住 `content_../..`（前缀是对的），却会误伤手工/工具建的目录名；
    而"单段 + 不以点开头"正好是我们要的安全边界，且不限制命名。
    """
    if not isinstance(upload_id, str):
        return None
    uid = upload_id.strip()
    if not uid or uid != os.path.basename(uid) or uid.startswith("."):
        return None                                  # 含路径分隔符 / 隐藏项 → 拒绝
    if uid.startswith("content_"):
        digest = uid[len("content_"):]
        if not _CONTENT_DIGEST_RE.match(digest):     # 指纹必须是纯十六进制
            return None
        return _content_dir(digest)
    return os.path.join(JOBS_ROOT, uid)


def _file_digest(file_path):
    """文件内容 sha1。分块读，别把大文件整块读进内存。"""
    digest = hashlib.sha1()
    with io.open(file_path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _content_meta(work):
    """读内容库里的 ``meta.json``；不存在或坏了返回 None。"""
    p = os.path.join(work or "", "meta.json")
    if not os.path.isfile(p):
        return None
    try:
        with io.open(p, encoding="utf-8") as handle:
            return json.load(handle)
    except Exception:
        return None


def _write_content_meta(work, meta):
    with io.open(os.path.join(work, "meta.json"), "w",
                 encoding="utf-8", newline="") as handle:
        json.dump(meta, handle, ensure_ascii=False, indent=2)


def _new_upload_temp(filename):
    """上传文件的临时落点：先落这里算指纹，再决定是否入库。"""
    tmp_dir = os.path.join(CONTENT_ROOT, "_tmp")
    os.makedirs(tmp_dir, exist_ok=True)
    return os.path.join(tmp_dir, uuid.uuid4().hex[:12] + _safe_ext(filename))


def _safe_ext(filename):
    """只保留扩展名里的字母数字与点，且截断 —— 上传文件名不可信。"""
    return re.sub(r"[^A-Za-z0-9.]", "", os.path.splitext(filename or "")[1])[:10]


def _remove_quietly(file_path):
    """尽力删一个文件（失败不抛）。用于清理上传临时件。"""
    try:
        os.remove(file_path)
    except OSError:
        pass


async def _receive_upload(upload, safe_name):
    """把上传流式落盘到临时文件，返回其路径。"""
    tmp = _new_upload_temp(safe_name)
    with io.open(tmp, "wb") as handle:
        while True:                                  # 流式，不整块读进内存
            chunk = await upload.read(1 << 20)
            if not chunk:
                break
            handle.write(chunk)
    return tmp


def _store_content(kind, tmp_path, filename):
    """把一个刚上传的文件放进内容库。

    返回 ``(内容 id, 内容目录, 是否命中已有内容, 入库后的绝对路径, meta)``。

    命中（同一份内容以前上传过）时**不覆盖**已存在的文件 —— 内容寻址的前提是
    不可变：指纹一样就说明内容一样，覆盖没有意义，还多一次写。
    """
    digest = _file_digest(tmp_path)
    short = digest[:12]
    work = _content_dir(short)
    os.makedirs(os.path.join(work, "input"), exist_ok=True)
    stored = os.path.join(work, "input", short + _safe_ext(filename))
    hit = os.path.isfile(stored)
    if not hit:
        shutil.move(tmp_path, stored)
    try:
        os.remove(tmp_path)                          # 命中时删临时文件；不命中已被搬走
    except OSError:
        pass

    meta = _content_meta(work) or {}
    names = list(meta.get("original_names") or [])
    if filename and filename not in names:
        names.append(filename)
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    meta.update({
        "sha1": digest, "sha1_12": short, "kind": kind,
        "stored_name": os.path.basename(stored),
        # 原名只作展示/追溯用：入库文件名必须是内容的函数，否则指纹不再代表内容
        "original_names": names[-8:],
        "size": os.path.getsize(stored),
        "updated_at": now,
    })
    meta.setdefault("created_at", now)
    _write_content_meta(work, meta)
    return "content_" + short, work, hit, stored, meta


def _result_dir(job):
    """任务的**产物目录** —— 读取侧的唯一入口。

    为什么不能直接拼 ``<job_dir>/result``：续跑（rerun）**不复制产物**，
    新任务只在 ``config.result_dir`` 里指向原任务的 result 目录
    （slides/audio 动辄几百 MB，复制没必要）。写入侧 `_build_argv` 一直用的是
    这个字段，但读取侧（产物列表 / 逐页索引 / 文件服务 / 总页数）原先各拼各的
    ``<job_dir>/result``，于是**续跑出来的任务看不到自己的产物** ——
    使用者得回头点原任务才能看（实测反馈）。
    """
    config = (job or {}).get("config") or {}
    declared = config.get("result_dir")
    if declared:
        return declared
    return os.path.join(_job_dir(job["id"]), "result")


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

# 音频同理。参考音频是喂给克隆模型的，拿错文件（比如把论文传成音频）必须在**上传时**
# 就挡住 —— 否则要等到 stage 2 合成语音时才炸，那时已经花掉 stage 1 的 API 费用。
_AUDIO_MAGIC = (
    (b"RIFF", "wav"),                 # 后面还要看第 8~12 字节是不是 WAVE
    (b"ID3", "mp3"),
    (b"fLaC", "flac"),
    (b"OggS", "ogg"),
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


def _sniff_audio(file_path):
    """返回 wav / mp3 / flac / ogg / m4a；不是音频则返回 None。

    m4a 没有统一的魔数，靠 ftpy/`ftyp` 品牌判断；wav 要额外确认第 8~12 字节是 WAVE
    （RIFF 容器也被 AVI/WEBP 用）。
    """
    try:
        with io.open(file_path, "rb") as handle:
            head = handle.read(32)
    except Exception:
        return None
    if head[:4] == b"RIFF" and head[8:12] == b"WAVE":
        return "wav"
    if head[:4] == b"RIFF" and head[8:12] == b"AVI ":
        return None
    for magic, name in _AUDIO_MAGIC:
        if name != "wav" and head.startswith(magic):
            return name
    if head[4:8] == b"ftyp":
        return "m4a"
    if len(head) >= 2 and head[0] == 0xFF and (head[1] & 0xE0) == 0xE0:
        return "mp3"                      # 无 ID3 标签的裸 MP3 帧同步
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


def _total_pages(result_dir):
    """总页数：优先从渲染出的 PDF 取，其次数幻灯片图。

    传的是**产物目录**（见 `_result_dir`）而不是任务目录 —— 续跑时产物在原任务那里，
    拼 `<job_dir>/result` 会数出 0 页。
    """
    pdf = os.path.join(result_dir, "slides_rendered.pdf")
    if os.path.isfile(pdf):
        try:
            import pymupdf
            doc = pymupdf.open(pdf)
            count = doc.page_count
            doc.close()
            return count
        except Exception:
            pass
    img_dir = os.path.join(result_dir, "slide_imgs")
    if os.path.isdir(img_dir):
        return len([f for f in os.listdir(img_dir) if f.endswith(".png")])
    return 0


# ──────────────────────────────────────────────────────────────
# 阶段模型 —— 阶段名 / 权重 / 有无逐页进度的**唯一来源**
#
# 为什么要有这张表（两个前端问题的共同根因）：
#   ① 界面上原先只有一个"阶段名"，而 `job.phase` 全流程只写 3 次
#      （排队中 → 启动中 → 已完成），过程阶段只能靠 progress.jsonl；
#      而前置结构化那一步不写进度 ⇒ 那 1~2 分钟必然显示"启动中"。
#   ② 进度条原先用「页数占比」当**总**进度：一个条要表达 6 个阶段，
#      在没有页数的阶段（幻灯片/讲稿/光标/合片）它只能停在上一个值上。
#      实测序列：stage1 0% → TTS 0→100% → 光标停在 100% → 数字人**回跳到 7%**
#      → 100% → 合片 100%。数字人那段更隐蔽：`talking_gen` 的批量路径跑完
#      N 页才逐页补报，14 条记录的时间戳**全是同一秒**（22:58:21），
#      所以真正在跑的 5~10 分钟里条一直满格。
#
# 把阶段、权重、有无页数集中在这里，好处是三件事只维护一处：
#   · 后端算**总进度**（前端只显示，不自己拼公式）；
#   · 前端按 key 渲染**步骤条**（已完成/进行中/未开始）；
#   · `/api/options` 下发给前端，避免前后端两处维护同一张表。
#
# 权重取自**实测耗时占比**（见本文件顶部 CALIBRATION 的两组真实计时）：
#   gkk_en 15 页：TTS 348.8 + 数字人 327.7 + 光标 2.6 + 合片 77.5 + 讲稿 111.3 = 868 s
#   gkk_zh 11 页：TTS 479.9 + 数字人 363.6 + 光标 17.2 + 合片 73.0            = 934 s
# 再补上 web 流程独有、命令行没有的两步：正文提取（本地，秒级）与
# LLM 结构化（PDF/Word 输入，实测 1~3 分钟）。
# 权重只是"进度条刻度"，不参与任何计算，换机器可重新标定。
PHASES = (
    # key,       中文名,        权重,  有计数进度, 计数单位, 无更新多久算卡住（秒）
    #
    # 关于权重：取自实测耗时占比，但要注意**光标阶段方差极大** ——
    # PDF 文本定位全命中时它只要几秒（实测 2.6s / 17.2s），
    # 而回退到 GUI-Owl 的指令每条约 30 秒（实测 18 条 ≈ 9 分钟，且其中一条异常到 6 分半）。
    # 所以给 0.05 并配合"第 k/N 条"的计数显示：静态权重表达不了这种方差，
    # 界面上的"第 k / N 条 + 已用 X 分钟"才是可信的那部分。
    ("extract",  "分析内容",     0.02, False, "",   180),
    ("struct",   "结构化正文",   0.10, False, "",   300),
    ("slides",   "生成幻灯片",   0.09, False, "",   300),
    ("script",   "生成讲稿",     0.05, True,  "页", 300),
    ("tts",      "合成语音",     0.29, True,  "页", 180),
    ("cursor",   "光标定位",     0.05, True,  "条", 600),
    ("talking",  "生成数字人",   0.32, True,  "页", 420),
    ("merge",    "合成视频",     0.06, False, "",   300),
    ("finish",   "收尾",         0.02, False, "",   300),
)
_PHASE_KEYS = [p[0] for p in PHASES]
_PHASE_LABEL = {p[0]: p[1] for p in PHASES}
_PHASE_WEIGHT = {p[0]: p[2] for p in PHASES}
_PHASE_HAS_COUNT = {p[0]: p[3] for p in PHASES}
_PHASE_UNIT = {p[0]: p[4] for p in PHASES}
_PHASE_STALL_LIMIT = {p[0]: p[5] for p in PHASES}
_PHASE_HAS_PAGES = _PHASE_HAS_COUNT          # 兼容旧名（语义已扩成"有计数"）
# 每个阶段的**起点刻度**（前面所有阶段的权重之和）—— 用来把"阶段内进度"嵌进总进度
_PHASE_START = {k: round(sum(p[2] for p in PHASES[:i]), 4)
                for i, k in enumerate(_PHASE_KEYS)}

# 阶段预计耗时（秒）—— **粗估**，只用来在界面上写"约 X 分钟"。
# 有页数的阶段从 CALIBRATION 的每页总耗时**按实测占比拆**（不另立一套数字，
# 否则两处标定会漂）：gkk_en 实测 868 s = TTS 40% + 数字人 38% + 光标/合片 9% + 讲稿 13%。
_PHASE_FIXED_S = {"extract": 20, "struct": 120, "slides": 60,
                  "cursor": 20, "merge": 80, "finish": 20}
_PHASE_PAGE_SHARE = {"tts": 0.40, "talking": 0.38}


def _phase_expected_s(phase_key, total_pages, script_lang):
    """某阶段的预计耗时（粗估）。取不到就返回 None，界面不显示。"""
    if phase_key in _PHASE_PAGE_SHARE:
        per_slide = CALIBRATION.get(
            "zh_per_slide_s" if script_lang == "zh" else "en_per_slide_s")
        if not (per_slide and total_pages):
            return None
        return int(per_slide * _PHASE_PAGE_SHARE[phase_key] * total_pages)
    if phase_key == "script":
        return None  # 逐页请求的耗时受模型、长度、并发影响；用本次完成速度估算剩余时间。
    return _PHASE_FIXED_S.get(phase_key)


def _phase_layout(include_talking=True):
    """返回当前任务真正会执行的阶段及归一化权重。"""
    keys = [k for k in _PHASE_KEYS if include_talking or k != "talking"]
    total_weight = sum(_PHASE_WEIGHT[k] for k in keys) or 1.0
    weights = {k: _PHASE_WEIGHT[k] / total_weight for k in keys}
    starts = {}
    cursor = 0.0
    for key in keys:
        starts[key] = cursor
        cursor += weights[key]
    return keys, weights, starts


def _phase_overall_pct(phase_key, done, total, include_talking=True):
    """把「当前阶段 + 阶段内页数」换算成**总进度百分比**。

    口径（刻意保守，宁可不动也不假装在走）：
      · 已完成阶段的权重全额计入；
      · 有逐页进度的阶段，按 done/total 线性计入自己的权重；
      · **没有逐页进度的阶段只计到它的起点** —— 不编造进度。
        这类阶段的"还活着"由界面上的「进行中 · 已用 X 分钟」表达。

    这样得到的总进度**单调不减**，也就消除了原先那次 100%→7% 的回跳。
    """
    keys, weights, starts = _phase_layout(include_talking)
    if phase_key not in keys:
        return 0
    base = starts[phase_key]
    weight = weights[phase_key]
    frac = 0.0
    if _PHASE_HAS_PAGES[phase_key] and total:
        frac = min(1.0, max(0.0, float(done) / float(total)))
    return min(100, int(round((base + weight * frac) * 100)))


def _phase_payload():
    """下发给前端的阶段表（步骤条按它渲染）。

    `unit` 是"这个阶段的计数单位"：语音/数字人是"页"，光标定位是"条"
    （它处理的是**指令**，不是页）。前端据此显示"第 k/N 条"而不是"页"。
    """
    return [{"key": k, "label": _PHASE_LABEL[k], "weight": _PHASE_WEIGHT[k],
             "has_pages": _PHASE_HAS_COUNT[k], "unit": _PHASE_UNIT[k]}
            for k in _PHASE_KEYS]


def _read_log_text(path):
    """读日志文本，**逐行**做编码回落（utf-8 → gbk）。

    为什么不能整文件用一种编码：`run.log` 里有两个写入者，编码不同 ——
    父进程用 UTF-8 写自己的行（`_log_line`），子进程的 stdout 默认按 Windows
    控制台编码（GBK）输出。实测这种混合文件**用 utf-8 与 gbk 解码都会抛
    UnicodeDecodeError**，而且按 utf-8 读出来是一堆替换字符：
      · 界面上的「日志尾巴」变成乱码；
      · 日志兜底解析彻底失效 —— 数「第 N 页合成完成」的正则命中 **0** 条。

    逐行回落可以同时兼容三种情况：新日志（已在 `_run_job` 里统一成 UTF-8）、
    历史混合日志、纯 GBK 老日志。逐行而不是整文件，是因为**同一文件里就混着两种编码**。
    """
    try:
        raw = open(path, "rb").read()
    except OSError:
        return ""
    lines = []
    for line in raw.split(b"\n"):
        for enc in ("utf-8", "gbk"):
            try:
                lines.append(line.decode(enc))
                break
            except UnicodeDecodeError:
                continue
        else:
            lines.append(line.decode("utf-8", errors="replace"))
    return "\n".join(lines)


def _read_tail(job_dir, count=6):
    """日志尾巴。

    无论用哪个进度来源都会读它 —— 它承载的是排查信息（报错、警告、
    护栏触发记录），不只是进度数字。
    """
    log_path = os.path.join(job_dir, "run.log")
    if not os.path.isfile(log_path):
        return []
    text = _read_log_text(log_path)
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

    info = {"source": "jsonl", "total_pages": 0, "counts": {},
            "phase_key": None, "phase_started_ts": None, "unknown_phase": None,
            "last_log_at": None, "stalled_seconds": None}
    for rec in records:
        phase = rec.get("phase")
        total = rec.get("total")
        done = rec.get("done")
        # ⚠️ `total_pages` 只能取**按页计数**的阶段（tts/talking）。
        # 光标阶段报的 total 是"指令条数"（实测 18），单位完全不同 ——
        # 早期把两者的 total 混在一起来取 max，会得到"18 页"这种荒谬结论，
        # 而界面上"第 k/18 页"看起来还挺正常，最难发现。
        pages = rec.get('total_pages') if phase == 'script' else total
        if phase in ("script", "tts", "talking") and isinstance(pages, int) and pages > 0:
            info["total_pages"] = max(info["total_pages"], pages)
        if isinstance(done, int):
            prev_done, prev_total = info["counts"].get(phase, (0, 0))
            info["counts"][phase] = (max(prev_done, done),
                                     max(prev_total, total if isinstance(total, int) else 0))

    # 未知 phase 要**显式**处理。以前是 `_PHASE_LABEL.get(x, "")`，能不能正常
    # 显示全靠上一层"空值不覆盖旧值"的巧合 —— 这里改成：认得就用，认不得就
    # 记下名字（供排查"流水线加了阶段、前端没跟上"），阶段仍保留 job 里那个。
    raw = records[-1].get("phase")
    if raw in _PHASE_WEIGHT:
        info["phase_key"] = raw
        for rec in records:                    # 当前阶段的**第一条**记录 = 阶段开始
            if rec.get("phase") == raw:
                info["phase_started_ts"] = rec.get("ts")
                break
    else:
        info["unknown_phase"] = raw

    ts = records[-1].get("ts")
    info['script_stats'] = records[-1] if raw == 'script' else None
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
    text = _read_log_text(log_path)

    info = {"source": "log",
            "tts_done": len(re.findall(r"第 \d+ 页合成完成", text)),
            "talking_done": len(re.findall(r"第 \d+ 页完成", text)),
            "total_pages": 0, "last_log_at": None, "stalled_seconds": None,
            "phase_key": None, "phase_started_ts": None, "pipeline_done": False}

    mtime = os.path.getmtime(log_path)
    info["last_log_at"] = time.strftime("%H:%M:%S", time.localtime(mtime))
    info["stalled_seconds"] = int(max(0, time.time() - mtime))

    # 兜底路径只判到"哪一大阶段"，够用了 —— 它存在的意义是老任务目录
    # （没有 progress.jsonl）在界面上不至于一片空白。
    # `pipeline_done` 是**兜底专用**的信号：日志已说整条链路跑完，但任务状态
    # 可能还没落盘（父进程收尾的那一小段窗口）。它只影响"显示文案"，
    # 不影响终态判定 —— 终态一律以 status 为准。
    if "数字人与幻灯片合成完成" in text or "subtitle_render] 完成" in text:
        info["phase_key"] = "finish"
        info["pipeline_done"] = True
    elif info["talking_done"]:
        info["phase_key"] = "talking"
    elif info["tts_done"]:
        info["phase_key"] = "tts"
    elif "start stage" in text:
        info["phase_key"] = "slides"
    return info


def read_progress(job):
    """把进度换算成前端要的形状（可直接推给前端）。

    三个来源，**优先结构化**：
      1. `progress.jsonl`   —— 流水线主动写，机器可读（首选）
      2. `run.log` + 正则   —— 零改造但脆的兜底（老任务目录）
      3. `job.phase_key`    —— **阶段边界**：结构化、启动等不经过流水线的步骤靠它
    终态（完成/失败/已取消）一律以**任务状态**为准，不从日志文本猜。

    两个字段专供总进度条，前端**不要自己算**：
      · `overall_pct` —— 阶段加权总进度（单调不减，口径见 `_phase_overall_pct`）
      · `phase_key` / `phase_index` —— 步骤条按 `/api/options` 的 `phases` 渲染
    """
    job_dir = _job_dir(job["id"])
    status = job.get("status")
    finished = status in ("done", "failed", "canceled", "awaiting_review")
    config = job.get("config") or {}
    include_talking = _as_bool(config.get("generate_talking"), True)
    active_phase_keys, _active_weights, _active_starts = _phase_layout(include_talking)

    info = {
        "stage": job.get("stage", 0),
        "phase_key": job.get("phase_key") or ("finish" if finished else "queued"),
        "phase": job.get("phase", "排队中"),
        "tts_done": 0, "talking_done": 0, "total_pages": 0,
        "last_log_at": None, "stalled_seconds": None,
        "tail": _read_tail(job_dir),
        "source": "none",
        "page_done": None, "page_total": None,
        "phase_elapsed_s": None, "phase_expected_s": None,
        "unknown_phase": None,
    }

    found = _progress_from_jsonl(job, job_dir) or _progress_from_log(job, job_dir)
    # 旧版课程任务只打印逐页完成日志：为正在运行／旧任务补齐计数与活动时间。
    if found and found.get('phase_key') == 'script' and not (found.get('counts') or {}).get('script', (0, 0))[1]:
        log_path = os.path.join(job_dir, 'run.log')
        if os.path.isfile(log_path):
            matches = re.findall(r'\[course\] 讲稿 (\d+)/(\d+) 已保存', _read_log_text(log_path))
            if matches:
                d, t = map(int, matches[-1])
                found.setdefault('counts', {})['script'] = (d, t)
                found['stalled_seconds'] = int(max(0, time.time() - os.path.getmtime(log_path)))
    info["counts"] = found.get("counts") if found else None
    if found:
        for key in ("tts_done", "talking_done", "total_pages"):
            if found.get(key):
                info[key] = found[key]
        # 各阶段的"第 k/N"计数（单位随阶段不同：页 / 条）
        counts = found.get("counts") or {}
        if counts.get("tts"):
            info["tts_done"] = counts["tts"][0]
        if counts.get("talking"):
            info["talking_done"] = counts["talking"][0]
        # 流水线报的阶段比 job.phase 细（job.phase 只有 排队中/启动中/已完成），
        # 有就用它推进阶段指针。
        if found.get("phase_key"):
            info["phase_key"] = found["phase_key"]
            info["phase"] = _PHASE_LABEL[found["phase_key"]]
        if found.get("unknown_phase"):
            info["unknown_phase"] = found["unknown_phase"]
        # 兜底路径看到"整条链路跑完"：只改显示文案。任务状态还是 running 时
        # （父进程收尾的窗口期）界面不该停在"生成数字人"。
        if found.get("pipeline_done") and not finished:
            info["phase"] = "已完成"
        info["source"] = found.get("source", info["source"])
        if found.get("last_log_at"):
            info["last_log_at"] = found["last_log_at"]
        if found.get("stalled_seconds") is not None:
            info["stalled_seconds"] = found["stalled_seconds"]
        if found.get("phase_started_ts"):
            info["phase_elapsed_s"] = int(max(0, time.time() - found["phase_started_ts"]))

    # 总页数兜底：两个来源都没给出时，从渲染出的 PDF / 幻灯片图数推断。
    # 注意用**产物目录**（续跑时产物在原任务那里，拼 job_dir 会数出 0 页）。
    if not info["total_pages"]:
        info["total_pages"] = _total_pages(_result_dir(job))

    if status in ("done", "failed", "canceled"):
        info["phase"] = {"done": "已完成", "failed": "失败",
                         "canceled": "已取消"}[status]
    if status == 'awaiting_review':
        info['phase'] = '课件与讲稿待审核'
        info['phase_key'] = 'script'
    if status == "done":
        # 收尾阶段与终态对齐：否则会出现"任务已完成、阶段却停在 merge"这种
        # 自相矛盾的对象（步骤条按 status 判定所以不显示错，但接口消费者会困惑）。
        info["phase_key"] = "finish"

    # ── 步骤条 + 总进度（前端的唯一数据源）──
    phase_key = info["phase_key"]
    if phase_key == "talking" and not include_talking:
        phase_key = "merge"
        info["phase_key"] = phase_key
        info["phase"] = _PHASE_LABEL[phase_key]
    if phase_key not in active_phase_keys:
        # job 里可能写着 "queued"/"startup" 这类非阶段值：归一到起点，
        # 界面文案仍用 job.phase（"排队中"），步骤条高亮第一步。
        phase_key = active_phase_keys[0]
        info["phase_key"] = phase_key
    info["phase_index"] = active_phase_keys.index(phase_key)
    info["phase_count"] = len(active_phase_keys)

    if _PHASE_HAS_COUNT[phase_key] and not finished:
        # 计数**按阶段各取各的**：tts/talking 单位是"页"，cursor 是"条"。
        # 早先只认 tts/talking，于是光标阶段在界面上永远只有"光标定位"四个字 ——
        # 实测 18 条回退指令跑了 9 分钟，界面完全看不出它在动（使用者报"卡住了"）。
        done, total = (info.get("counts") or {}).get(phase_key, (0, 0))
        info["count_done"] = done
        info["count_total"] = total or None
        info["count_unit"] = _PHASE_UNIT[phase_key]
        # 兼容字段：前端旧代码读 page_*；语义同 count_*。
        info["page_done"] = done
        info["page_total"] = total or None

    # 总进度用**当前阶段自己的**计数与总数（页/条各自算，见上面的 counts）
    computed = _phase_overall_pct(phase_key,
                                  info.get("count_done") or 0,
                                  info.get("count_total") or info["total_pages"],
                                  include_talking=include_talking)
    if status == "done":
        info["overall_pct"] = 100
    elif finished:
        # 失败/取消：**保留已走到的位置**（比清零有用 —— 一眼看出挂在哪个阶段），
        # 但封顶 99%，避免"进度 100%"与"任务失败"同时出现这种自相矛盾的画面。
        info["overall_pct"] = min(99, computed)
    else:
        info["overall_pct"] = computed

    info["phase_expected_s"] = _phase_expected_s(
        phase_key, info["total_pages"], config.get("script_lang", "en"))
    info['phase_remaining_s'] = None
    if phase_key == 'script' and not finished:
        stats = (found or {}).get('script_stats') or {}
        generated, elapsed = stats.get('generated_done', 0), stats.get('generation_elapsed_s', 0)
        remaining = max(0, (info.get('count_total') or 0) - (info.get('count_done') or 0))
        if generated > 0 and elapsed > 0:
            info['phase_remaining_s'] = round(elapsed / generated * remaining)
        info['script_concurrency'] = stats.get('concurrency') or config.get('script_concurrency', 1)

    # 卡住判据**按阶段给上限**：数字人阶段本来就是"几分钟没有新记录"，
    # 用同一个阈值会在整个数字人阶段持续误报「疑似卡住」。
    limit = _PHASE_STALL_LIMIT.get(phase_key, 300)
    info["stalled_limit_s"] = limit
    info["stalled"] = bool(not finished
                           and info.get("stalled_seconds") is not None
                           and info["stalled_seconds"] > limit)

    # 兼容字段：旧前端/旧测试读的是 tts_done/talking_done，保留不动。
    info["total"] = info["total_pages"]
    info["done"] = info["page_done"] or 0
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

    ⚠️ `--if_tree_search` 以前是写死的常量，现在**从配置读、默认 False**。
    读而不是写死之后有两条连带要求（都不是可选的）：
      · 它必须进 `artifact_cache` 的指纹字段表 —— 它改变幻灯片产物；
      · 值必须归一成 bool —— 否则 True/"True"/1 会算出三个不同的缓存键。
    默认值刻意与上游 argparse 的 default=True **相反**：上游默认开，
    而这步每张溢出页要多一次带图 VLM 调用，收益没实测过，所以默认不开。
    """
    cfg = job["config"]
    argv = [
        VENV_PY, "-u", "pipeline.py",
        "--stage", json.dumps(cfg.get("stages", ["1", "2", "3"])),
        # result_dir 允许被显式指定：续跑时**直接复用原任务的 result 目录**，
        # 这样 stage 1 已生成的幻灯片与讲稿就地重用，不需要复制几 GB 的产物。
        "--result_dir", cfg.get("result_dir") or os.path.join(_job_dir(job["id"]), "result"),
        "--paper_latex_root", job["paper_latex_root"],
        "--slide_lang", cfg["slide_lang"],
        "--script_lang", cfg["script_lang"],
        "--paper_type", "auto",
        "--slide_count", str(cfg.get("slide_count", 12)),
        "--model_name_t", cfg.get('text_model') or os.environ.get('PAPER2VIDEO_LLM_MODEL', 'deepseek-flash'),
        "--model_name_v", cfg.get('vision_model') or os.environ.get('PAPER2VIDEO_VLM_MODEL', 'deepseek-flash'),
        "--script_concurrency", str(cfg.get('script_concurrency', 3)),
        "--cursor_style", cfg.get('cursor_style', cursor_styles.DEFAULT),
        "--cursor_scale", str(cfg.get('cursor_scale', 1)),
        "--talking_profile", cfg.get("talking_profile", "musetalk-local"),
        "--gpu_list", "0",
        # 必须显式传：上游 argparse 的默认值是 True，不传就等于每篇都开。
        "--if_tree_search",
        "True" if _as_bool(cfg.get("if_tree_search"), False) else "False",
        "--generate_talking",
        "True" if _as_bool(cfg.get("generate_talking"), True) else "False",
    ]
    if job.get("ref_img"):
        argv += ["--ref_img", job["ref_img"]]
    prompt_file = cfg.get("script_prompt_path")
    if prompt_file:
        argv += ["--script_prompt_file", prompt_file]
    slide_prompt_file = cfg.get("slide_prompt_path")
    if slide_prompt_file:
        argv += ["--slide_prompt_file", slide_prompt_file]
    if cfg.get("reuse_script"):
        argv += ["--reuse_script", "True"]
    if cfg.get("require_reuse_script"):
        argv += ["--require_reuse_script", "True"]
    if cfg.get("course_plan_path"):
        argv += ["--course_plan", cfg["course_plan_path"]]
    argv += ["--review_before_render", "True" if cfg.get("review_before_render") else "False"]
    if cfg.get("regenerate_pages") is not None:
        argv += ["--regenerate_pages", json.dumps(cfg["regenerate_pages"])]
    if cfg.get('script_generation_id'):
        argv += ['--script_generation_id', cfg['script_generation_id']]
    # 原始论文文件：流水线会从它提取插图放进工程（幻灯片里才有 \includegraphics，
    # 树搜索也才可能有活干）。源文件不可用时流水线会给出可读提示、不影响出片。
    # ⚠️ 刻意**不**把它放进产物指纹 —— 它对应的"图集"由 `figures` 字段表示。
    source = job.get("source_path")
    if source and os.path.isfile(source):
        argv += ["--paper_source", source]
    # 音色：把解析好的参考音频/参考文本透传给流水线（pipeline.py 里
    # `args.ref_audio or lang["ref_audio"]` 会优先用我们给的这份）。
    # ⚠️ 只传 ref_audio、不传 ref_text 时，pipeline 必须保持 ref_text=None，
    # 让 speech_gen 去 ASR 转写；若让它回落到语言档位的文本，就会拿"另一段音频
    # 的文本"去配这段音频，克隆保真度直接崩。这条语义在 pipeline.py 里显式写了。
    if cfg.get("ref_audio"):
        argv += ["--ref_audio", cfg["ref_audio"]]
        if cfg.get("ref_text"):
            argv += ["--ref_text", cfg["ref_text"]]
    return argv


def _as_bool(value, default=False):
    """把表单/JSON 里的开关值归一成真正的 bool。

    为什么必须有这一步：`if_tree_search` 会进产物指纹，而 ``True`` / ``"True"`` /
    ``"true"`` / ``1`` 在指纹里是**四个不同的值** —— 行为完全相同却互相不命中，
    症状是"缓存时灵时不灵"，比彻底不命中难查得多。
    前端传布尔、CLI 传字符串，两种都要能接住。
    """
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in ("1", "true", "yes", "on", "y"):
        return True
    if text in ("0", "false", "no", "off", "n", ""):
        return False
    return default


def _artifact_facts(job):
    """算产物缓存指纹用的「与输出有关的事实」。

    ⚠️ 加字段前先问一句：**它会改变产物吗？**
       会 → 必须加。少一个字段 = 输入不同却误命中 = **静默产出错误内容**
            （"数字人跳过判据不含音频"那次就是这个病）。
       不会 → 别加。多一个字段 = 输入变了其实结果一样却重算 = 白花一次钱。
    """
    job_dir = _job_dir(job["id"])
    cfg = job.get("config") or {}
    paper_text = os.path.join(job_dir, "ingest", "paper_text.md")
    main_tex = os.path.join(job.get("paper_latex_root") or "", "main.tex")
    return {
        # 结构化看的是「提取出来的正文」；.tex/.zip 输入没有正文时退回原始上传件
        # 的内容指纹（两个都由内容库保证稳定，不会每次变）。
        "source_sha1": (artifact_cache.digest_file(paper_text)
                        or job.get("source_digest")
                        or artifact_cache.digest_file(main_tex)),
        # 幻灯片与讲稿看的是「结构化产出的 main.tex」
        "main_tex_sha1": artifact_cache.digest_file(main_tex),
        "paper_type": "auto",
        # 相同正文的授课结构化使用独立系统提示词，不能混用原论文缓存。
        "struct_mode": "course" if cfg.get("course_plan_path") else "paper",
        "slide_lang": cfg.get("slide_lang"),
        "script_lang": cfg.get("script_lang"),
        # 自定义讲稿提示词会直接改变 subtitle_w_cursor.txt，必须进入 deck 指纹。
        # 存哈希而不是全文：manifest 可审计，同时避免缓存元数据膨胀。
        "script_prompt_sha1": (cfg.get("script_prompt_sha1")
                                or artifact_cache.digest_file(
                                    cfg.get("script_prompt_path"))),
        "slide_prompt_sha1": (cfg.get("slide_prompt_sha1")
                               or artifact_cache.digest_file(
                                   cfg.get("slide_prompt_path"))),
        "slide_count": int(cfg.get("slide_count", 12)),
        "course_plan_sha": cfg.get("course_plan_sha"),
        "llm_model": cfg.get('text_model') or os.environ.get("PAPER2VIDEO_LLM_MODEL", "deepseek-flash"),
        "vlm_model": cfg.get('vision_model') or os.environ.get('PAPER2VIDEO_VLM_MODEL', 'deepseek-flash'),
        "model_api_base": cfg.get('model_api_base') or os.environ.get('OPENAI_BASE_URL') or os.environ.get('OPENAI_API_BASE'),
        "script_generation_version": 2,
        "talking_profile": cfg.get("talking_profile", "musetalk-local"),
        # 幻灯片布局微调（树搜索）：它真的改变 stage 1 的幻灯片产物
        # （溢出页多出 4 个宽度候选、由 VLM 选一张），所以**必须在指纹里**。
        # 归一成 bool —— 见 _as_bool 的说明。
        "if_tree_search": _as_bool(cfg.get("if_tree_search"), False),
        # 论文配图：幻灯片里能不能出现 \includegraphics 完全取决于"工程里有哪些图"。
        # 所以它改变 stage 1 的产物 ⇒ **必须进指纹**。否则"给同一个工程补上图片"
        # 之后会命中"没有图的那一版"缓存，产物与预期不符且不报错。
        "figures": _figures_signature(job),
    }


def _figures_signature(job):
    """图片集的指纹。两种形态，都必须**确定**（不能返回 None）：

      · `own:v<版本>:n<数量>:<清单哈希>` —— 工程**自带**的、被 `\\includegraphics`
        真正引用的图（上游用法：使用者给的 LaTeX 工程）
      · `v<版本>`                         —— 其余情况（我们自己从源文件提取，或者没有图）

    ⚠️ 为什么"我们自己提取"这一支只记版本号、**不去读 `figures/index.json`**：
    图集是 `(源文件, 提取器版本)` 的确定函数，而源文件已经由 `source_sha1` /
    `main_tex_sha1` 覆盖在键里了，所以只需版本号保证"提取逻辑升级了就失效"。
    读 index.json 会强行要求"提图必须先于指纹计算"，于是写入点在 web 层、
    真正用图的人在流水线里（复制后的目录）—— 两个位置必然漂移。
    实测这个漂移真的会发生：`copy_folder` 只在目标不存在时复制，
    重跑/续跑时副本是旧的、拿不到 web 层新写的图。现在提图只在流水线里做一次。
    """
    root = job.get("paper_latex_root") or ""
    if os.path.isdir(root):
        own = [p for p in paper_figures.project_image_files(root)
               if not p.startswith(paper_figures.FIGURES_DIRNAME + os.sep)]
        if own:
            payload = []
            for rel in own:
                try:
                    payload.append("{}:{}".format(rel, os.path.getsize(os.path.join(root, rel))))
                except OSError:
                    payload.append(rel)
            digest = hashlib.sha1("\n".join(payload).encode("utf-8")).hexdigest()[:12]
            return "own:v{}:n{}:{}".format(paper_figures.FIGURES_VERSION, len(own), digest)
    return "v{}".format(paper_figures.FIGURES_VERSION)


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
        _log_line(log, "[struct] （续跑旧任务时若素材已被清理，请重新上传内容文件）")
        return False

    argv = [VENV_PY, "-u", os.path.join(SCRIPTS, "paper_struct.py"),
            source, "--out", job_dir, '--model', job['config'].get('text_model') or os.environ.get('PAPER2VIDEO_LLM_MODEL', 'deepseek-flash')]
    if job.get("config", {}).get("course_plan_path"):
        argv += ["--text-file", os.path.join(job_dir, "ingest", "paper_text.md")]
    _log_line(log, "[struct] 正在把 {} 转成 LaTeX 工程（一次 API 调用，约 1~2 分钟）".format(
        os.path.basename(source)))
    # 阶段要显式推进两次：提取正文是本地的（秒级），结构化才要 API 调用。
    # 不写这两行的话，这 1~2 分钟界面只能显示"启动中"。
    await _set_phase(job["id"], "extract")
    proc = await asyncio.create_subprocess_exec(
        *argv, cwd=REPO_ROOT, stdout=log, stderr=asyncio.subprocess.STDOUT, env=env)
    _CURRENT["proc"] = proc
    _CURRENT["job_id"] = job["id"]
    await _set_phase(job["id"], "struct")
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


async def _set_phase(job_id, phase_key):
    """推进任务阶段（同时写机器可读的 phase_key 与中文文案）。

    为什么需要这个入口：`job.phase` 原先全流程只在 3 处被写
    （排队中 → 启动中 → 已完成），中间过程阶段**完全依赖 progress.jsonl**；
    而前置的结构化不经过流水线、不写进度文件 ⇒ 那 1~2 分钟界面只能显示
    "启动中"（使用者实测反馈："启动后一直显示启动中，很迷惑"）。
    """
    if phase_key not in _PHASE_WEIGHT:
        return None
    return await _update_job(job_id, phase_key=phase_key,
                             phase=_PHASE_LABEL[phase_key])


async def _run_job(job):
    """在子进程里跑流水线，日志落盘供进度解析。"""
    job_id = job["id"]
    job_dir = _job_dir(job_id)
    log_path = os.path.join(job_dir, "run.log")
    argv = _build_argv(job)
    keys = _JOB_KEYS.get(job_id) or {}

    job["status"] = "running"
    job["phase"] = "启动中"
    # 起始阶段：PDF/Word 要先把正文转 LaTeX（结构化），.tex 输入直接进 stage 1。
    # 这两条路径的"等待形态"完全不同，界面上必须能区分开。
    job["phase_key"] = "extract" if job.get("needs_struct") else "slides"
    job["argv"] = argv
    _save_job(job)

    env = dict(os.environ)
    # P1：告诉流水线把结构化进度写到哪里。未设置该环境变量时，
    # 流水线**不生成任何额外文件**（命令行直接跑 pipeline.py 行为完全不变）。
    env["PAPER2VIDEO_PROGRESS_FILE"] = os.path.join(job_dir, "progress.jsonl")
    # run.log 的编码必须统一。父进程用 UTF-8 写自己的行（_log_line），而子进程的
    # stdout 默认按 **Windows 控制台编码（GBK）** 输出 —— 两种编码混在同一个文件里，
    # 实测 utf-8 与 gbk **解码都会抛 UnicodeDecodeError**，后果是：
    #   · 界面上的日志尾巴全是乱码（"第 5 页合成完成" → "�� 5 ҳ�ϳ����"）；
    #   · 日志兜底解析失效（数页数的正则命中 0 条）。
    # 只设 PYTHONIOENCODING 是**不够的**（这条早先的判断已被实测推翻，见下）。
    env["PYTHONIOENCODING"] = "utf-8"
    # ⚠️ 2026-09-19 起同时设 `PYTHONUTF8=1`（UTF-8 模式）。早先刻意不设它，
    # 理由是"把影响限制在标准流上、不去改变 `open()` 的默认编码"—— 那个判断错了：
    # 上游代码里**大量 `open()` 假设 UTF-8**（提示词模板、中间 .tex、JSON），
    # 而 Windows 上 `open()` 默认用 ANSI(GBK)。实测撞上的是
    # `slide_code_gen_select_improvement.improve_layout` 读 `prompts/select_proposal.txt`
    # （内含 `→`）时的 `UnicodeDecodeError: 0x92`，只在 `--if_tree_search True` 时才走到，
    # 所以默认关的时候一直没暴露。全仓库同类写法扫描到 92 处（含 evaluation/ 那棵子树）。
    #
    # 逐个改当然更"显式"，但那只覆盖我们**已经发现**的那些；UTF-8 模式一条覆盖全部，
    # 而且**让 Windows 开发机的行为对齐 Linux 服务器**（正是本项目的迁移目标）。
    # 我们用到的那些关键位置仍然逐处写死 `encoding="utf-8"`（双保险，也让命令行直跑
    # `pipeline.py` 不依赖这个环境变量）。
    env["PYTHONUTF8"] = "1"
    # 凭据来自**这个任务自己**那份（_JOB_KEYS[job_id]），不是全局会话 ——
    # 多人共用同一服务时不会互相覆盖。
    if keys.get("api_key"):
        env["OPENAI_API_KEY"] = keys["api_key"]
        env["OPENAI_COMPATIBLE_MODEL_API_KEY"] = keys["api_key"]
    model_base = keys.get('api_base') or job['config'].get('model_api_base')
    if model_base:
        env["OPENAI_API_BASE"] = model_base
        env["OPENAI_BASE_URL"] = model_base
        env["OPENAI_COMPATIBLE_MODEL_API_BASE"] = model_base

    started = time.time()
    # 产物缓存（阶段 3）的决策与结果，最后会记进任务记录。
    cache_only = False
    cache_info = {"struct_hit": False, "deck_hit": False, "deck_script_hit": False}
    with io.open(log_path, "w", encoding="utf-8", errors="replace") as log:
        log.write("### 命令 {}\n".format(" ".join(argv)))
        # 凭据来源必须写进日志：**回落路径要可见**，否则"没配 key 的机器上静默
        # 用错凭据"这种问题在开发机（.env 里有 key）永远暴露不出来。
        if keys.get("api_key"):
            _log_line(log, "[keys] 使用本任务随请求提供的 API key（{}）".format(
                _mask(keys["api_key"])))
        else:
            _log_line(log, "[keys] ⚠️ 本任务没有随请求提供 API key，"
                           "将使用进程环境里的 OPENAI_API_KEY（来源可能是仓库根的 .env）")
        # 开了树搜索必须留痕：它是唯一会**额外花钱**的开关，而它的表现是
        # "这次明显更慢 + 多出几次调用"。事后没人能解释那次耗时的时候，
        # 这一行就是唯一的答案。
        if _as_bool((job.get("config") or {}).get("if_tree_search"), False):
            _log_line(log, "[toggle] 已开启「幻灯片布局微调（树搜索）」—— "
                           "每张内容溢出的幻灯片会多 4 次编译 / 4 次渲染 / "
                           "1 次带图 VLM 调用，本次耗时与费用都会高于默认值")
        # 音色用了哪个、还是回落了 —— 必须写进日志：
        # 使用者选了"冰糖"却听到原来的男声时，唯一能自证的就是这一行。
        _log_line(log, "[voice] {}".format(
            (job.get("config") or {}).get("voice_note") or "内置默认音色"))
        _log_line(log, "[talking] {}".format(
            (job.get("config") or {}).get("talking_note")
            or ("已启用数字人" if _as_bool(
                (job.get("config") or {}).get("generate_talking"), True)
                else "已关闭数字人")))
        log.flush()

        # ── 产物缓存：命中结构化 ⇒ 这一次不调 LLM ──
        # 为什么必须在 `_prepare_latex` **之前**：那一步本身就要花钱，
        # 放在它后面等于"钱已经花了才说可以省"，命中就失去意义了。
        facts = _artifact_facts(job)
        cache_info["struct_key"] = artifact_cache.struct_key(facts)
        force = bool((job.get("config") or {}).get("force_regenerate"))
        if force:
            _log_line(log, "[cache] 本次要求强制重新生成（force_regenerate），不使用缓存")
        elif job.get("needs_struct"):
            cached_tex, _cdir, why = artifact_cache.lookup_struct(
                facts, key=cache_info["struct_key"])
            if cached_tex:
                target = os.path.join(job["paper_latex_root"], "main.tex")
                os.makedirs(os.path.dirname(target), exist_ok=True)
                shutil.copy2(cached_tex, target)
                job["needs_struct"] = False           # 已拿到 main.tex，前置步骤不必跑
                cache_info["struct_hit"] = True
                _log_line(log, "[cache] 结构化命中：{} → 跳过这次 LLM 调用".format(why))
            else:
                _log_line(log, "[cache] 结构化未命中（{}），本次会调用 LLM".format(why))
            log.flush()

        # 前置：PDF / Word 输入先转成 LaTeX 工程（见 _prepare_latex 的说明）
        prepared = True
        if job.get("needs_struct"):
            prepared = await _prepare_latex(job, job_dir, log, env)

        # ── 产物缓存：命中幻灯片/讲稿 ⇒ 只跑剩余的本地下游 ──
        # 讲稿是**唯一必须联网付费**的 stage 2 步骤（VLM 要读全部幻灯片，
        # 实测占一篇 token 的 76%）；TTS/光标/数字人/合片全是本地计算。
        entry = None
        # main.tex 现在应该在位了 —— 在这里**定下 deck 指纹**，之后的查询与写入都用它。
        # 不能等到写入时再算：流水线有可能改写 main.tex，那样"写入键 ≠ 查询键"，
        # 缓存就永远命中不了，而且没有任何报错（静默失效是最难查的一种）。
        facts = _artifact_facts(job)
        cache_info["deck_key"] = artifact_cache.deck_key(facts)
        if prepared and not force and "1" in (job["config"].get("stages") or []):
            entry, cdir, why = artifact_cache.lookup_deck(
                facts, key=cache_info["deck_key"])
            if entry and entry.get("slides"):
                cfg = job["config"]
                want = list(cfg.get("stages") or ["1", "2", "3"])
                copied = artifact_cache.reuse_into(cdir, _result_dir(job), entry)
                want = [s for s in want if s != "1"]  # 幻灯片已就位，不必再生成
                if "2" in want:
                    cfg["reuse_script"] = bool(entry.get("script"))
                    _log_line(log, "[cache] 讲稿{}：stage 2 的 VLM 调用{}".format(
                        "命中" if entry.get("script") else "未命中",
                        "已跳过（本次无讲稿 API 费用）" if entry.get("script")
                        else "仍要执行（要花一次 API）"))
                cfg["stages"] = want
                argv = _build_argv(job)               # stages 变了，命令行要重建
                job["argv"] = argv
                cache_info["deck_hit"] = True
                cache_info["deck_script_hit"] = bool(entry.get("script"))
                _log_line(log, "[cache] {}；复用 {} 个文件。实际命令 {}".format(
                    why, len(copied), " ".join(argv)))
                cache_only = not want                 # 要的东西全从缓存来 → 不必起进程
            else:
                _log_line(log, "[cache] 幻灯片/讲稿未命中（{}），本次照常生成".format(why))
            log.flush()

        if not prepared:
            # 前置步骤失败：不跑流水线，但仍走下面的收尾写入，
            # 让任务落到 failed 且带上 exit_code（否则状态会一直停在 running）。
            code = 1
        elif cache_only:
            code = 0
            _log_line(log, "[cache] 本次无需运行流水线：要的产物已全部从缓存复用")
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

        # ── 缓存回写（best-effort）：产物在就存，存不上也不影响出片 ──
        # 刻意**不看退出码**：即使后面某步失败了，已经生成好的幻灯片与讲稿依然值钱
        # —— 下次重试就省下那次 API。缺哪个存哪个。
        if not force:
            main_tex = os.path.join(job.get("paper_latex_root") or "", "main.tex")
            paper_text = os.path.join(job_dir, "ingest", "paper_text.md")
            if os.path.isfile(main_tex) and os.path.isfile(paper_text):
                # key= 传查询时那个键，别在这里重算（见 artifact_cache.publish_struct 说明）
                got = artifact_cache.publish_struct(
                    _artifact_facts(job), main_tex, job_id,
                    key=cache_info.get("struct_key"))
                if got:
                    _log_line(log, "[cache] 已存结构化产物 → {}".format(got))
            got = None if job.get("resumed_from") else artifact_cache.publish_deck(
                _artifact_facts(job), _result_dir(job), job_id,
                key=cache_info.get("deck_key"))
            if got:
                cache_info["published_deck"] = got
                _log_line(log, "[cache] 已存幻灯片/讲稿 → {}".format(got))
            log.flush()

    # ⚠️ 必须走 _update_job：它会先重新读盘再改，避免用旧快照覆盖
    #    取消接口刚写下的 status=canceled（实测踩过，见 _JOB_LOCK 处的注释）。
    async with _JOB_LOCK:
        final = _load_job(job_id)
        final["exit_code"] = code
        final["elapsed_min"] = round((time.time() - started) / 60, 1)
        # 产物缓存的决策与回写结果：`struct_key`/`deck_key` 同时是
        # artifact_cache.referenced_keys() 判断"这条缓存还有没有人在用"的依据。
        final["cache"] = cache_info
        # ⚠️ 命中缓存会改动 config（stages / reuse_script）并据此重建 argv，这些
        #    **必须落盘**。否则任务记录里还是"请求时的样子"、实际跑的却是另一套参数
        #    —— 事后没人能解释"为什么这次没跑 stage 1"（日志里有，但记录里查不到）。
        final["config"] = job.get("config", final.get("config"))
        final["argv"] = job.get("argv", final.get("argv"))
        if final.get("status") == "canceled":
            # 取消是使用者的明确意图，不该被退出码改写；
            # 但退出码与耗时仍要记下来 —— 排查时有用，且能证明收尾确实执行过。
            final["phase"] = "已取消"
        else:
            awaiting = code == 0 and final["config"].get("review_before_render")
            final["status"] = "awaiting_review" if awaiting else "done" if code == 0 else "failed"
            final["phase"] = "课件与讲稿待审核" if awaiting else "已完成" if code == 0 else "失败"
            # 成功才把阶段推到 finish；**失败时保留最后那个阶段** ——
            # 步骤条上就能直接看出"挂在哪一步"，比只写一个"失败"有用得多。
            if code == 0:
                final["phase_key"] = "script" if awaiting else "finish"
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
    return {
        "cursors": cursor_styles.options(),
        "presets": presets,
        "slide_langs": [{"value": k, "label": v["desc"]} for k, v in lc.SLIDE_LANGS.items()],
        "script_langs": [{"value": k, "label": v["desc"]} for k, v in lc.SCRIPT_LANGS.items()],
        "accept_formats": [".tex", ".zip", ".docx", ".doc", ".pdf"],
        "slide_prompt_max_chars": SLIDE_PROMPT_MAX_CHARS,
        "slide_count": {"default": 12, "min": SLIDE_COUNT_MIN, "max": SLIDE_COUNT_MAX},
        # 内容类型只决定“怎么讲”，不会改变输入格式。每个模板都把完整提示词
        # 下发给前端，使用者可以在保持机器可读输出格式的前提下继续编辑。
        "script_prompt_profiles": [
            {
                "value": key,
                "label": value["label"],
                "desc": value["desc"],
                "prompts": {
                    "zh": _script_prompt_text(key, "zh"),
                    "en": _script_prompt_text(key, "en"),
                },
                "slide_prompts": {
                    "zh": _slide_prompt_text(key, "zh"),
                    "en": _slide_prompt_text(key, "en"),
                },
            }
            for key, value in _SCRIPT_PROMPT_PROFILES.items()
        ],
        "script_prompt_max_chars": SCRIPT_PROMPT_MAX_CHARS,
        # 阶段表由后端下发（前端不硬编码）：步骤条与总进度共用同一份定义，
        # 将来加阶段只改 app.py 一处，前端自动跟上。
        "phases": _phase_payload(),
        # 音色：候选**按讲稿语言过滤**（跨语言克隆音色会漂），并告知功能是否可用 ——
        # 没配 MIMO_API_KEY 时界面要把"生成音色"禁用并说明原因，
        # 而不是等使用者点了才报错（也顺手把"参考文本是什么"告诉使用者）。
        "voice": {
            "available": mimo_voice.available(),
            "by_lang": {lang: mimo_voice.voices_for(lang) for lang in ("zh", "en")},
            "ref_texts": mimo_voice.REF_TEXTS,
            "limits": {"min_s": mimo_voice.MIN_SECONDS, "max_s": mimo_voice.MAX_SECONDS},
            "accept_audio": [".wav", ".mp3", ".m4a", ".flac", ".ogg"],
        },
        # 可选开关由后端下发（前端只渲染，别硬编码）：文案、默认值、成本口径
        # 都跟着实现走 —— 开关的语义最容易在前后端各写一份之后悄悄分叉。
        "toggles": [
            {
                "key": "if_tree_search",
                "label": "幻灯片布局微调（树搜索）",
                "default": False,
                "note": "可选的版面优化步骤，默认关闭",
                "hint": "编译后用 Overfull 警告定位内容溢出的页，为每页生成 4 个图片宽度"
                        "候选（×1 / 0.75 / 0.5 / 0.25），由视觉模型挑一张再重排版。"
                        "会明显多花时间与调用：每张溢出页 4 次 LaTeX 编译 + 4 次渲染 "
                        "+ 1 次带图 VLM 调用。关掉不影响出片，只是不做这步事后微调。",
                "cost": "每张溢出页 +4 次编译 / +4 次渲染 / +1 次带图 VLM 调用",
            },
        ],
    }


@app.get('/api/cursors/{style}.png')
async def api_cursor_asset(style: str):
    try:
        asset, _ = cursor_styles.resolve(style)
    except ValueError:
        raise HTTPException(404, detail='光标素材不存在')
    if not asset or not asset.is_file():
        raise HTTPException(404, detail='光标素材不存在')
    return FileResponse(asset, media_type='image/png')


class ModelListRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    api_key: str = Field(min_length=1, max_length=2000)
    api_base: str = Field(default='', max_length=2000)


def _model_base(value):
    from urllib.parse import urlsplit
    if value is not None and (not isinstance(value, str) or len(value) > 2000 or any(ord(c) < 32 for c in value)):
        raise HTTPException(422, detail='api_base：必须为不超过 2000 字符的服务地址')
    base = (value or '').strip().rstrip('/')
    if base:
        try:
            parsed = urlsplit(base)
            parsed.port  # 非数字或越界端口应在发请求前返回字段错误。
        except ValueError:
            raise HTTPException(422, detail='api_base：服务地址无效')
        if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or any(c.isspace() for c in base):
            raise HTTPException(422, detail='api_base：请填写不含凭据、查询参数的 HTTP(S) 服务地址')
    return base


@app.post('/api/models')
async def api_models(payload: ModelListRequest, request: Request):
    _local_write(request)
    base = _model_base(payload.api_base)
    def fetch():
        import pv_env  # noqa: F401
        from openai import OpenAI
        with OpenAI(api_key=payload.api_key.strip(), base_url=base or None, timeout=15, max_retries=0) as client:
            data = client.models.list().data
            return sorted({m.id for m in data if isinstance(m.id, str) and 0 < len(m.id) <= 200 and not any(ord(c) < 32 for c in m.id)})[:500]
    try:
        ids = await asyncio.to_thread(fetch)
        return {'ok': True, 'models': ids, 'message': '已获取 {} 个模型；服务商列表通常不标注视觉能力，请确认讲稿模型支持图像。'.format(len(ids))}
    except Exception as exc:
        code = getattr(exc, 'status_code', None)
        message = '鉴权失败，请检查 API Key' if code in (401, 403) else '该服务未提供可读取的模型列表，或连接超时；可以手动输入模型 ID。'
        return {'ok': False, 'models': [], 'message': message}


class ModelVerifyRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    api_key: str = Field(default='', max_length=2000)
    api_base: str = Field(default='', max_length=2000)
    model: str = Field(default='deepseek-flash', min_length=1, max_length=200)


@app.post("/api/key/verify")
async def api_key_verify(payload: ModelVerifyRequest, request: Request):
    """提交前自检：区分「欠费 / 鉴权失败 / 网络不通」。

    为什么要专门做这个：DeepSeek 余额耗尽时**不返回明确错误码**，
    而是返回空响应或截断内容 —— 症状与"网关抖动"完全一样，
    之前就据此误判过一轮。所以这里必须把三类失败分开报。
    """
    import pv_env  # noqa: F401
    from openai import OpenAI

    _local_write(request)
    api_key = payload.api_key.strip()
    api_base = _model_base(payload.api_base)
    config = {'text_model': payload.model}
    _normalize_models(config)
    if not api_key:
        return {"ok": False, "kind": "missing", "message": "未填写 API Key"}

    def verify():
        with OpenAI(api_key=api_key, base_url=api_base or None, timeout=30, max_retries=0) as client:
            return client.chat.completions.create(
                model=config['text_model'], messages=[{"role": "user", "content": "ping"}],
                max_tokens=64, temperature=0)
    try:
        resp = await asyncio.to_thread(verify)
        if not resp.choices or not (resp.choices[0].message.content or '').strip():
            return {'ok': False, 'kind': 'empty', 'message': '接口未返回可用文字，请检查模型能力、输出限制或服务额度'}
        usage = getattr(resp, "usage", None)
        # ⚠️ 自检**不再往服务端存 key**（原先这里写 _SESSION_KEYS）。
        # 自检用的是本次请求带来的凭据，真正的注入发生在建任务时 ——
        # 那时才把 key 挂到**那个 job_id** 上（见 _remember_keys / _run_job）。
        # 这样"自检别人的 key"不会污染任何任务的执行凭据。
        return {"ok": True, "kind": "ok",
                "message": "连接正常",
                "masked": _mask(api_key),
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
        return {"ok": False, "kind": kind, "message": msg}


@app.post("/api/ingest")
async def api_ingest(paper: UploadFile = File(...)):
    """上传内容文件 → 提取正文 → 返回预览。

    这是「转换预览」这一步的后端：把 main.tex 的章节结构 + 正文开头
    给使用者确认，确认无误再开跑 —— 因为转换错了下游无法补救。

    **内容寻址（2026-09-19）**：同一份文件上传两次会落到**同一个内容目录**，
    第二次直接复用已提取的正文，不再落第二份盘、也不再重复解析。
    可复用的前提是「文件内容 + 提取器版本」都一致 —— 缺了版本号这一项，
    改了预处理逻辑却仍命中旧缓存（内容对不上且不报错）。
    """
    import paper_ingest as pi

    safe_name = os.path.basename(paper.filename or "upload.bin")
    tmp = await _receive_upload(paper, safe_name)
    try:
        content_id, work, existed, src, meta = _store_content("paper", tmp, safe_name)
    except Exception:
        try:
            os.remove(tmp)                       # 入库失败别留临时文件
        except OSError:
            pass
        raise

    text_file = os.path.join(work, "ingest", "paper_text.md")
    cached = bool(existed
                  and meta.get("extractor_version") == pi.EXTRACTOR_VERSION
                  and os.path.isfile(text_file))

    if cached:
        with io.open(text_file, encoding="utf-8", errors="replace") as handle:
            text = handle.read()
        print("[ingest] 内容命中（{}），复用已提取的正文：{}".format(
            content_id, os.path.basename(src)))
    else:
        try:
            text, extracted = pi.ingest(src, os.path.join(work, "ingest"))
        except Exception as exc:
            shutil.rmtree(os.path.join(work, "ingest"), ignore_errors=True)
            if not existed:
                # 内容目录是这次刚建的，而且这个 id **还没被返给任何人** ⇒
                # 不可能有任务引用它，可以安全删掉（不留空壳，原代码的意图）。
                # `existed` 为真时绝不能删：老任务的 source_path 指在里面。
                shutil.rmtree(work, ignore_errors=True)
            return JSONResponse(status_code=400, content={
                "ok": False,
                "error": "{}: {}".format(type(exc).__name__, exc),
                "hint": "支持 .tex / .zip / .docx / .doc / .pdf",
            })
        # 提取结论与版本号一起记进 meta：下次命中就靠它判断"能不能复用"
        meta.update(dict(extracted or {}))
        meta["extractor_version"] = pi.EXTRACTOR_VERSION
        _write_content_meta(work, meta)

    return {
        "ok": True,
        "ingest_id": content_id,
        "source_path": src,
        # 让界面能告诉使用者"这份论文之前传过，直接复用了提取结果"
        "cached": cached,
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

    **内容寻址（2026-09-19）**：同一张图上传两次落到同一个内容目录。
    这条不只是省磁盘 —— 入库文件名是内容的函数（`<sha1[:12]><扩展名>`），
    而数字人的产物路径里**含参考图的主文件名**，所以"同样的图 ⇒ 同样的产物路径"，
    跨任务的产物复用才可能行得通。
    """
    safe_name = os.path.basename(paper.filename or "portrait.png")
    tmp = await _receive_upload(paper, safe_name)

    fmt = _sniff_image(tmp)
    if fmt is None:
        try:
            os.remove(tmp)                            # 失败不留任何残留
        except OSError:
            pass
        return JSONResponse(status_code=400, content={
            "ok": False,
            "error": "这不是一张能识别的图片（按文件头判断，不看扩展名）",
            "hint": "支持 PNG / JPEG / WEBP / BMP",
        })

    content_id, work, existed, dest, content_meta = _store_content(
        "portrait", tmp, safe_name)

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
        "portrait_id": content_id,
        "portrait_path": dest,
        "cached": existed,
        "format": fmt,
        "width": width,
        "height": height,
        "size_mb": round(os.path.getsize(dest) / 2 ** 20, 2),
        # 界面上显示原名（入库文件名是指纹，直接给用户看没意义）
        "original_name": (content_meta.get("original_names") or [safe_name])[-1],
        "warnings": warnings,
        "note": "未做人脸检测（本机 OpenCV 已移除级联分类器）。请自行确认："
                "正脸、单人、无遮挡；侧脸或多人会让数字人环节失败或效果很差。",
    }


# ──────────────────────────────────────────────────────────────
# 音色：MiMo 生成参考音频 / 上传自己的音频
#
# 架构见 docs/音色方案.md —— **只用 MiMo 生成一段参考音频**，逐页合成仍走本地
# Qwen3-TTS 克隆。于是"音色"最终就是一份 wav + 它的参考文本，三种来源
# （预置 / 描述设计 / 上传）都归到同一对字段上，流水线完全不需要知道区别。
# ──────────────────────────────────────────────────────────────
@app.post("/api/voices/generate")
async def api_voice_generate(payload: dict):
    """生成（或命中缓存取回）一段参考音频，供界面**试听**。

    两种情况下都不额外消耗配额：同一个音色第二次请求会命中缓存；
    生成失败也只是返回 ok=False，界面照常能选别的音色。
    `force=True` 对应界面上的"重新生成" —— 设计音色有随机性，需要多掷几次。
    """
    kind = (payload.get("kind") or "preset").strip()
    lang = (payload.get("lang") or "zh").strip()
    try:
        data = mimo_voice.generate(kind, voice=payload.get("voice"),
                                   prompt=payload.get("prompt"), lang=lang,
                                   force=bool(payload.get("force")))
    except mimo_voice.VoiceError as exc:
        # 用 200 + ok=False：这是"可预期的业务失败"（没配 key / 限流 / 网络），
        # 不是接口坏了 —— 前端要按 kind 给出可操作的提示，而不是弹一个通用错误。
        return JSONResponse(status_code=200, content={
            "ok": False, "error_kind": exc.kind,
            "error": exc.message, "detail": exc.detail})
    return {
        "ok": True,
        "key": data["key"],
        "cached": bool(data.get("cached")),
        "duration_s": data.get("duration_s"),
        "ref_text": data["ref_text"],
        "sample_url": "/api/voices/{}/sample?v={}".format(
            data["key"], os.stat(data["ref_audio"]).st_mtime_ns),
        "script_lang": data["lang"],
        "kind": data["kind"],
        "voice": data.get("voice"),
        "prompt": data.get("prompt"),
    }


@app.get("/api/voices/{key}/sample")
async def api_voice_sample(key: str):
    """试听：直接回参考音频本身。

    为什么是"给音频"而不是"再合成一句话"：这份音频就是这个音色的**定义**，
    听到它=听到最终音色（逐页合成只是把同一音色套到别的文本上）。
    """
    if "/" in key or "\\" in key or ".." in key:
        raise HTTPException(status_code=400, detail="非法的音色 id")
    path = os.path.join(mimo_voice.CACHE_DIR, key + ".wav")
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="音色不存在或已被清理：{}".format(key))
    return FileResponse(path, media_type="audio/wav", filename=key + ".wav")


@app.post("/api/voices/upload")
async def api_voice_upload(paper: UploadFile = File(...), ref_text: str = Form("")):
    """上传自己的参考音频（可附参考文本）。

    与 `/api/portrait` 同一套做法（流式落盘 + 按文件头判断 + 失败删空壳），
    但多两步 —— 目的都是"别等到 stage 2 合成语音时才炸"：

      1. **用 ffmpeg 解码成 24kHz 单声道 wav**。一举三得：证明它真能解码、
         统一成克隆模型需要的格式、拿到可测的时长（mp3/m4a 用 soundfile 未必读得了）。
      2. 时长必须落在 3~30 秒。太短克隆不稳定（宁可明确拒绝，也不要产出一个
         人声音色乱漂的成片），太长只是白等。
    """
    safe_name = os.path.basename(paper.filename or "voice.wav")
    # 临时落盘即可：算完指纹、解完码就删 —— 音色自身的缓存本来就是内容寻址的
    # （`jobs/_voices/upload_<sha1[:16]>.wav`），没必要再留一份原始上传件。
    src = await _receive_upload(paper, safe_name)

    fmt = _sniff_audio(src)
    if fmt is None:
        _remove_quietly(src)
        return JSONResponse(status_code=400, content={
            "ok": False,
            "error": "这不是一段能识别的音频（按文件头判断，不看扩展名）",
            "hint": "支持 WAV / MP3 / M4A / FLAC / OGG",
        })

    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        _remove_quietly(src)
        return JSONResponse(status_code=400, content={
            "ok": False, "error": "找不到 ffmpeg，无法解码音频（流水线本身也依赖它）",
        })

    with io.open(src, "rb") as handle:
        digest = hashlib.sha1(handle.read()).hexdigest()[:16]
    os.makedirs(mimo_voice.CACHE_DIR, exist_ok=True)
    dest = os.path.join(mimo_voice.CACHE_DIR, "upload_{}.wav".format(digest))
    proc = subprocess.run(
        [ffmpeg, "-y", "-v", "error", "-i", src,
         "-ac", "1", "-ar", "24000", "-c:a", "pcm_s16le", dest],
        capture_output=True, text=True, timeout=300)
    _remove_quietly(src)
    if proc.returncode != 0 or not os.path.isfile(dest):
        tail = (proc.stderr or "").strip().splitlines()[-3:]
        return JSONResponse(status_code=400, content={
            "ok": False, "error": "这段音频解不开（ffmpeg 解码失败）",
            "detail": " / ".join(tail)[:300]})

    duration = None
    try:
        import soundfile
        duration = float(soundfile.info(dest).duration)
    except Exception:
        pass
    if duration is not None and duration < mimo_voice.MIN_SECONDS:
        os.remove(dest)
        return JSONResponse(status_code=400, content={
            "ok": False,
            "error": "音频只有 {:.1f} 秒，太短（至少 {:.0f} 秒）".format(
                duration, mimo_voice.MIN_SECONDS),
            "hint": "参考音频越接近你要的说话风格越好；建议 5~15 秒、单人、无背景音乐",
        })

    text = (ref_text or "").strip()
    txt_path = os.path.join(mimo_voice.CACHE_DIR, "upload_{}.txt".format(digest))
    if text:
        with io.open(txt_path, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
    elif os.path.isfile(txt_path):                 # 换了内容但沿用同一指纹的旧文本
        os.remove(txt_path)

    warnings = []
    if not text:
        # 不给参考文本时流水线会退回 ASR 转写；这条**必须**提前说清楚，
        # 因为实测 Qwen3-ASR 对参考音频会转出错文本，而错的 ref_text 直接拉低克隆保真度。
        warnings.append("未提供参考文本：将由 ASR 自动转写，实测**可能不准**（建议手填）")
    if duration is not None and duration > mimo_voice.MAX_SECONDS:
        warnings.append("时长 {:.1f} 秒偏长（超过 {:.0f} 秒只会让克隆编码变慢）"
                        .format(duration, mimo_voice.MAX_SECONDS))

    meta = {
        "key": "upload_{}".format(digest), "kind": "upload", "lang": None,
        "filename": safe_name, "source_format": fmt,
        "duration_s": round(duration, 2) if duration else None,
        "ref_text": text or None, "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    with io.open(os.path.join(mimo_voice.CACHE_DIR, "upload_{}.json".format(digest)),
                 "w", encoding="utf-8", newline="") as handle:
        json.dump(meta, handle, ensure_ascii=False, indent=2)
    return {
        "ok": True,
        "spec": {"kind": "upload", "path": dest, "ref_text": text or None},
        "key": meta["key"],
        "duration_s": meta["duration_s"],
        "sample_url": "/api/voices/{}/sample".format(meta["key"]),
        "warnings": warnings,
    }


@app.post("/api/jobs")
async def api_create_job(payload: dict):
    """创建任务 → 入队。同一时刻只会有一个在跑（单卡）。"""
    ingest_id = payload.get("ingest_id")
    # 走**唯一入口**解析往哪找上传内容：它同时认新老两种 id（老的 ingest_<随机>
    # 目录还在、老任务也还指着），并挡住 `../` 这类穿越（使用者传的字段不可信）。
    work = _upload_work(ingest_id)
    if not work or not os.path.isdir(work):
        raise HTTPException(status_code=400, detail="请先上传内容文件（缺少有效的 ingest_id）")

    # 创建任务时也接受凭据（使用者可能没点"自检"就直接开跑）。
    # 凭据挂到**本次任务的 id** 上（见下面 _remember_keys），不是全局会话 ——
    # 所以"谁的任务用谁的 key"。仍然只放进程内存，不落盘。

    config = dict(payload.get("config") or {})
    _normalize_models(config)
    config['model_api_base'] = _model_base(payload.get('api_base')) or os.environ.get('OPENAI_BASE_URL') or os.environ.get('OPENAI_API_BASE')

    # 数字人是可选增强项。即使界面勾着开关，只要没有有效人像就自动关闭，
    # stage 3 仍会继续做“幻灯片 + 音频 + 光标 + 字幕”的完整合片。
    portrait = payload.get("portrait_path")
    portrait = portrait if portrait and os.path.isfile(portrait) else None
    generate_talking = _as_bool(config.get("generate_talking"), bool(portrait))
    if generate_talking and not portrait:
        generate_talking = False
        talking_note = "未上传有效人像，已自动跳过数字人；仍会生成无数字人的完整讲解视频"
    elif generate_talking:
        talking_note = "已启用数字人"
    else:
        talking_note = "已关闭数字人；将生成幻灯片讲解视频"
    config["generate_talking"] = generate_talking
    config["talking_note"] = talking_note

    # 先校验再创建任务目录/记凭据：无效输入必须是零副作用的 400，
    # 不能留下一个永远不会入队的 job_<时间> 孤儿目录。
    content_profile, script_prompt = _normalize_script_prompt(config)
    slide_prompt, slide_count = _normalize_slide_prompt(config, content_profile)
    config["paper_type"] = "auto"
    plan = selected_text = None
    if config.get("course") is not None:
        try:
            plan, selected_text = course.build_plan(_course_source(work), config["course"], slide_count, config.get("script_lang", "zh"))
        except ValueError as exc:
            raise HTTPException(422, detail=str(exc))
        if payload.get("course_plan_token") != plan["token"]:
            raise HTTPException(409, detail="教案或材料已变化，请重新预览并确认")
    config["review_before_render"] = True
    config["stages"] = ["1", "2"]
    config.pop("regenerate_pages", None)
    config.pop('script_generation_id', None)
    config.pop("course_plan_path", None)

    job_id = "job_" + time.strftime("%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:4]
    _remember_keys(job_id, payload)          # 凭据跟着任务走，不是全局一份
    job_dir = _job_dir(job_id)
    os.makedirs(job_dir, exist_ok=True)

    # 讲稿提示词按任务落成独立 UTF-8 文件，避免把上万字符塞进 Windows 命令行；
    # job.json 只记路径与哈希，缓存与追溯都以这份不可混淆的任务快照为准。
    script_prompt_path = os.path.join(job_dir, "script_prompt.txt")
    with io.open(script_prompt_path, "w", encoding="utf-8", newline="") as handle:
        handle.write(script_prompt + "\n")
    config["content_profile"] = content_profile
    config["script_prompt_path"] = script_prompt_path
    config["script_prompt_sha1"] = artifact_cache.digest_text(script_prompt)
    config.pop("script_prompt", None)
    slide_prompt_path = os.path.join(job_dir, "slide_prompt.txt")
    with io.open(slide_prompt_path, "w", encoding="utf-8", newline="") as handle:
        handle.write(slide_prompt + "\n")
    config["slide_prompt_path"] = slide_prompt_path
    config["slide_prompt_sha1"] = artifact_cache.digest_text(slide_prompt)
    config["slide_count"] = slide_count
    config.pop("slide_prompt", None)

    # 音色：把界面上的选择解析成「参考音频 + 参考文本」。
    # **任何失败都回落到内置音色**，绝不因为音色问题拦住任务 —— 音色是增强，
    # 而 MiMo 是"限时免费"的第三方服务，网络/配额/key 都可能出问题。
    # 回落原因会同时进 config、响应体与任务日志（回落必须可见，见 _run_job）。
    voice_spec = config.get("voice") or {"kind": "default"}
    ref_audio, ref_text, voice_note = mimo_voice.resolve(
        voice_spec, config.get("script_lang", "en"))
    config["voice"] = voice_spec
    config["voice_note"] = voice_note
    if ref_audio:
        config["ref_audio"] = ref_audio
        # ref_text 可能是 None（上传音频没给文本）—— 不要在这里编一个，
        # 让流水线退回 ASR 转写，而不是拿语言档位的文本去配另一段音频。
        config["ref_text"] = ref_text
    shutil.copytree(os.path.join(work, "ingest"), os.path.join(job_dir, "ingest"),
                    dirs_exist_ok=True)
    if plan:
        config["course_plan_path"] = os.path.join(job_dir, "course_plan.json")
        course.write_json(config["course_plan_path"], plan)
        config["course_plan_sha"] = plan["token"]
        config["target_minutes"] = plan["target_minutes"]
        Path(job_dir, "ingest", "paper_text.md").write_text(selected_text, encoding="utf-8")
    config.pop("course", None)

    meta_path = os.path.join(job_dir, "ingest", "paper_text.md")
    needs_llm = os.path.isfile(meta_path)

    job = {
        "id": job_id,
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "status": "queued",
        "phase": "排队中",
        # 机器可读的阶段（中文 phase 只用于显示）—— 步骤条与总进度靠它
        "phase_key": "queued",
        "queue_ahead": _QUEUE.qsize(),
        "config": config,
        "ref_img": portrait,
        "paper_latex_root": (None if plan else payload.get("paper_latex_root")) or os.path.join(job_dir, "latex_proj"),
        "needs_struct": needs_llm,
        # 原始上传文件的路径。needs_struct 时前置步骤要用它做结构化；
        # 放在这里而不是去 jobs/<ingest_id>/input 里找，是因为 ingest 目录
        # 将来可能被清理，而任务自己记着更可靠。
        "source_path": payload.get("source_path"),
        "source_name": ((_content_meta(work) or {}).get("original_names") or [None])[-1],
        # 内容寻址的溯源信息：论文与音色的内容指纹（音色本身就是 sha1 命名的 wav）。
        # 记下来的用处：将来把"同样的输入 ⇒ 同样的产物"做成缓存键时，这些字段
        # 就是现成的上半截；排查时也能一眼看出"这个任务用的是哪一份文件"。
        "ingest_id": ingest_id,
        "source_digest": (_content_meta(work) or {}).get("sha1"),
    }
    _save_job(job)
    await _QUEUE.put(job_id)
    return {"ok": True, "job_id": job_id, "queue_ahead": _QUEUE.qsize(),
            "queued": True,
            # 把这个任务的音色结论回给界面：用了哪个音色、还是回落到了内置默认。
            # 不返回的话，使用者只能从成片里"听"出来自己选的音色没生效。
            "voice_note": voice_note,
            "voice_active": bool(ref_audio),
            "generate_talking": generate_talking,
            "talking_note": talking_note}


def _rerun_plan(job):
    """只读预检；与流水线共用讲稿校验，确认后再次检查产物版本与共享目录占用。"""
    result = _result_dir(job)
    slide_dir = os.path.join(result, "slide_imgs")
    slide_names = sorted(n for n in os.listdir(slide_dir) if n.lower().endswith(".png")) \
        if os.path.isdir(slide_dir) else []
    slides_ready = bool(slide_names) and all(
        os.path.isfile(os.path.join(slide_dir, "{}.png".format(i)))
        for i in range(1, len(slide_names) + 1))
    script_file = os.path.join(result, "subtitle_w_cursor.txt")
    text, reason, reusable = "", "没有可复用的讲稿，需要重新生成（可能产生 API 费用）", False
    if os.path.isfile(script_file):
        try:
            with io.open(script_file, encoding="utf-8") as handle:
                text = handle.read()
            reusable, invalid = validate_script_output(text, len(slide_names))
            reusable = reusable and bool(text.strip()) and slides_ready
            reason = "讲稿校验通过，可复用讲稿并重做配音和视频" if reusable else \
                "讲稿无法复用：{}；重新生成可能产生 API 费用".format(invalid or "幻灯片不完整")
        except (OSError, UnicodeError):
            reason = "讲稿文件无法读取，重新生成可能产生 API 费用"
    can_rerun = job.get("status") in ("done", "failed", "canceled", "awaiting_review") and slides_ready
    if not slides_ready:
        reason = "幻灯片尚未完整生成，请新建任务重新生成内容"
    elif job.get("status") not in ("done", "failed", "canceled", "awaiting_review"):
        reason = "原任务尚未结束，暂不能重做"
    result_key = os.path.normcase(os.path.realpath(result))
    for other in _list_jobs():
        if other.get("status") in ("running", "queued") and \
                os.path.normcase(os.path.realpath(_result_dir(other))) == result_key:
            can_rerun = False
            reason = "有运行或排队任务正在使用同一份产物，请等待其结束后再重做"
            break
    versions = []
    for name in slide_names:
        stat = os.stat(os.path.join(slide_dir, name))
        versions.append((name, stat.st_size, stat.st_mtime_ns))
    token = hashlib.sha256(json.dumps({"id": job["id"], "status": job.get("status"),
        "config": job.get("config"), "script": text, "slides": versions},
        sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
    return {"job_id": job["id"], "source_name": job.get("source_name") or "",
            "can_rerun": can_rerun, "can_reuse_script": reusable,
            "reason": reason, "token": token,
            "overwrite": ["配音", "光标数据", "数字人视频", "合成视频"],
            "generate_talking": _as_bool((job.get("config") or {}).get("generate_talking"), True)}


@app.get("/api/jobs/{job_id}/rerun-plan")
async def api_rerun_plan(job_id: str):
    return _rerun_plan(_load_job(job_id))


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
    if payload.get("plan_token") or _as_bool(payload.get("require_reuse_script"), False):
        plan = _rerun_plan(src_job)
        if not plan["can_rerun"]:
            raise HTTPException(status_code=409, detail=plan["reason"])
        if payload.get("plan_token") != plan["token"]:
            raise HTTPException(status_code=409, detail="原任务产物已变化，请关闭确认框并重新检查")
        if _as_bool(payload.get("reuse_script"), True) and not plan["can_reuse_script"]:
            raise HTTPException(status_code=409, detail=plan["reason"])
    # 用 _result_dir 而不是拼 job_dir/result：这样"续跑的续跑"也能找到真正的产物目录
    # （否则第二次续跑会指向第一个续跑任务的空 result 目录）。
    src_result = _result_dir(src_job)
    if not os.path.isdir(src_result):
        raise HTTPException(status_code=400,
                            detail="原任务没有 result 目录，无法续跑（可能从未开始）")

    config = dict(src_job.get("config") or {})
    for field in ('text_model', 'vision_model', 'script_concurrency', 'cursor_style', 'cursor_scale'):
        if payload.get(field) is not None:
            config[field] = payload[field]
    _normalize_models(config)
    if payload.get('api_base'):
        config['model_api_base'] = _model_base(payload['api_base'])
    portrait = payload.get("portrait_path") or src_job.get("ref_img")
    portrait = portrait if portrait and os.path.isfile(portrait) else None
    generate_talking = _as_bool(
        payload.get("generate_talking"),
        _as_bool(config.get("generate_talking"), bool(portrait)))
    if generate_talking and not portrait:
        generate_talking = False
        config["talking_note"] = (
            "续跑时找不到有效人像，已自动跳过数字人；仍会生成无数字人的完整讲解视频")
    elif generate_talking:
        config["talking_note"] = "已启用数字人"
    else:
        config["talking_note"] = "已关闭数字人；将生成幻灯片讲解视频"
    config.update({
        "stages": payload.get("stages") or ["2", "3"],
        "reuse_script": _as_bool(payload.get("reuse_script"), True),
        "require_reuse_script": _as_bool(payload.get("reuse_script"), True)
            and bool(payload.get("plan_token")),
        "result_dir": src_result,
        "generate_talking": generate_talking,
        "review_before_render": not _as_bool(payload.get("reuse_script"), True),
    })
    config.pop("regenerate_pages", None)
    config.pop('script_generation_id', None)
    if not config['reuse_script']:
        config['script_generation_id'] = uuid.uuid4().hex
    if payload.get("review_pages") is not None:
        config["regenerate_pages"] = payload["review_pages"]
        config["review_before_render"] = True

    new_id = "job_" + time.strftime("%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:4]
    # 续跑的凭据：优先用请求里带的（前端有本地保存），否则沿用原任务那份。
    # 两者都没有时**不猜** —— _run_job 会明确记一行日志说明回落到了环境变量。
    _remember_keys(new_id, _JOB_KEYS.get(job_id))
    _remember_keys(new_id, payload)
    new_dir = _job_dir(new_id)
    os.makedirs(new_dir, exist_ok=True)
    src_ingest = os.path.join(_job_dir(job_id), "ingest")
    if os.path.isdir(src_ingest):                 # ingest 很小（正文），复制一份
        shutil.copytree(src_ingest, os.path.join(new_dir, "ingest"),
                        dirs_exist_ok=True)

    if _as_bool(payload.get('refresh_script_prompt'), False) and not config['reuse_script']:
        prompt = _script_prompt_text(config.get('content_profile', 'paper'), config.get('script_lang', 'zh'))
        target = os.path.join(new_dir, 'script_prompt.txt')
        Path(target).write_text(prompt, encoding='utf-8')
        config.update(script_prompt_path=target, script_prompt_sha1=artifact_cache.digest_file(target))

    job = {
        "id": new_id,
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "status": "queued",
        "phase": "排队中",
        "phase_key": "queued",
        "queue_ahead": _QUEUE.qsize(),
        "config": config,
        "ref_img": portrait,
        "paper_latex_root": src_job.get("paper_latex_root"),
        # 续跑时 LaTeX 工程一定已经存在（stage 1 跑过），不要再触发一次结构化
        "needs_struct": False,
        "source_path": src_job.get("source_path"),
        "source_name": src_job.get("source_name"),
        "resumed_from": job_id,
    }
    _save_job(job)
    await _QUEUE.put(new_id)
    return {"ok": True, "job_id": new_id, "resumed_from": job_id,
            "stages": config["stages"], "reuse_script": config["reuse_script"],
            "generate_talking": generate_talking,
            "queue_ahead": _QUEUE.qsize()}


@app.get("/api/jobs")
async def api_list_jobs():
    return {"jobs": _list_jobs()}


@app.get("/api/cache")
async def api_cache():
    """产物缓存现状（只读）。

    ⚠️ 刻意**不提供删除**：缓存目录可能被多个任务引用，删除前必须先问"谁还在用"
    —— 本仓库踩过"按前缀批量删"的事故。这里只给清点与「无人引用」的候选清单，
    删不删、删哪些由人来定。
    """
    info = artifact_cache.status()
    info["referenced"] = artifact_cache.plan_gc(JOBS_ROOT)
    return info


@app.get("/api/jobs/{job_id}")
async def api_get_job(job_id: str):
    job = _load_job(job_id)
    job["progress"] = read_progress(job)
    return job


def _sse_frames(job_id, is_disconnected, keepalive):
    """SSE 的帧序列（与 HTTP 传输解耦）。

    为什么单独抽成生成器：`TestClient` 对流式响应是**缓冲**的（要等响应结束才返回），
    用它测"空闲时发心跳"只能靠"让测试挂住 + 超时断言"，既慢又不可靠 —— 实测把整个
    测试套件卡死在这里。拆出来之后，测试可以直接 `__anext__()` 取前几帧，
    测的是**真实的帧内容**，不依赖传输层实现。

    `is_disconnected` 是一个同步可调用对象（HTTP 请求传 `request.is_disconnected`）。
    """
    async def gen():
        async def client_gone():
            """判断客户端是否已断开 —— 同时支持 async 与同步两种入参。

            ⚠️ 必须显式判断可等待性：HTTP 请求传进来的 `request.is_disconnected`
            是 **async** 方法，直接 `if is_disconnected():` 拿到的是一个
            **永远为真的 coroutine 对象** ⇒ 帧序列在第一帧之前就 break，
            接口返回 200 却一个事件都没有（现有冒烟测试抓到了这个：
            "SSE 收到事件 — 0 个"）。而测试里传的是同步 lambda，也不能写死 await。
            """
            value = is_disconnected()
            if inspect.isawaitable(value):
                value = await value
            return bool(value)

        last = None
        last_sent = time.time()
        while True:
            if await client_gone():
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
                "phase_key": job.get("phase_key"),
                "progress": read_progress(job),
                "exit_code": job.get("exit_code"),
                "elapsed_min": job.get("elapsed_min"),
                "queue_ahead": job.get("queue_ahead") if job.get("status") == "queued" else 0,
                # 续跑关系要推给前端：续跑任务的产物与原任务**共享**，
                # 界面上得说清楚，否则用户不知道产物在哪、也想不到"重跑会覆盖原任务的那份"。
                "resumed_from": job.get("resumed_from"),
                "config": {key: (job.get("config") or {}).get(key) for key in (
                    "generate_talking", "content_profile", "slide_lang", "script_lang",
                    "slide_count", "voice_note", "talking_note", "review_before_render", "target_minutes")},
            }
            body = json.dumps(payload, ensure_ascii=False)
            if body != last:                       # 只在变化时推，减少噪音
                yield "data: {}\n\n".format(body)
                last = body
                last_sent = time.time()
            elif time.time() - last_sent >= keepalive:
                # 注释帧：`:` 开头，客户端（EventSource）不会当消息处理，仅用于保活
                yield ": keep-alive\n\n"
                last_sent = time.time()
            if job.get("status") in ("done", "failed", "canceled", "awaiting_review"):
                break
            await asyncio.sleep(2)

    return gen()


@app.get("/api/jobs/{job_id}/events")
async def api_job_events(job_id: str, request: Request):
    """SSE 进度流。

    长任务（15~25 分钟）必须用流式推送；页面刷新后重连也能接着看，
    状态一律以后端为准，不依赖前端内存。

    两个坑（都踩过）：
      · **心跳必须有**。一个 14 页的任务，`progress.jsonl` 只在每页结束时写，
        中间可能几十秒没有任何变化；若某段时间连 tail 都没变（例如数字人批量
        阶段原来根本不写进度），这条连接就会长时间空闲，被中间层按空闲超时掐掉。
        心跳用 SSE 注释帧（": keep-alive"），客户端**不会**当消息处理，
        但能让连接保持活跃。
      · 断开时**前端要能重连**。原先前端在 onerror 里直接 close()，等于关掉了
        EventSource 自带的重连；一旦断流，前端里那个唯一的"status 变化"触发点
        就再也不来，产物区跟着冻结（使用者反馈"要手动刷新页面"）。
    """
    keepalive = float(os.environ.get("PAPER2VIDEO_SSE_KEEPALIVE") or 15)
    return StreamingResponse(
        _sse_frames(job_id, request.is_disconnected, keepalive),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


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
    base = _result_dir(job)                    # 续跑时指向原任务的 result
    items = []
    if os.path.isdir(base):
        for name in sorted(os.listdir(base)):
            path = os.path.join(base, name)
            if os.path.isfile(path):
                stat = os.stat(path)
                items.append({
                    "name": name,
                    "size_mb": round(stat.st_size / 2 ** 20, 2),
                    "size_bytes": stat.st_size,
                    # 文件仍在写入时大小/mtime 会变化。前端把版本放进视频 URL，
                    # 避免浏览器一直持有第一次读到的未封口 MP4。
                    "version": "{}-{}".format(stat.st_size, stat.st_mtime_ns),
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
    job = _load_job(job_id)                             # 任务不存在则 404
    base = _result_dir(job)                             # 续跑时指向原任务的 result

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
        "chapters": course.read_json(os.path.join(base, "chapters.json"), []),
    }


@app.get("/api/jobs/{job_id}/files/{rel_path:path}")
async def api_job_file(job_id: str, rel_path: str):
    """产物文件服务。FileResponse 自带 Range 支持，视频才能拖进度条。"""
    root = _result_dir(_load_job(job_id))        # 续跑时指向原任务的 result
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
    parser.add_argument("--lan-subnet", action="append",
                        default=[value.strip() for value in os.environ.get("P2V_WEB_LAN_SUBNETS", "").split(",") if value.strip()],
                        help="允许访问和操作任务的可信 IPv4 私有子网，可重复指定")
    args = parser.parse_args()
    try:
        app.state.lan_networks = tuple(_lan_network(value) for value in args.lan_subnet)
    except ValueError as exc:
        parser.error(str(exc))
    if args.host != "localhost" and not _trusted_address(args.host) and not (args.host == "0.0.0.0" and app.state.lan_networks):
        parser.error("非本机监听必须显式指定 --lan-subnet 可信子网")

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
    if app.state.lan_networks:
        print("可信局域网共享：{}（子网内设备可操作任务和读取产物）".format(", ".join(map(str, app.state.lan_networks))))
    else:
        print("（只监听本机）")
    if not os.path.isdir(_STATIC):
        print("提示：{} 不存在，当前只有 API。执行 `npm run build` 后重启即可看到界面。".format(
            os.path.relpath(_STATIC, os.path.dirname(os.path.abspath(__file__)))))
    uvicorn.run(app, host=args.host, port=args.port, log_level="info", proxy_headers=False)


if __name__ == "__main__":
    main()
