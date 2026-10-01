"""产物缓存：按「输入指纹」寻址，跳过已经付过费的步骤（阶段 3）。

为什么需要
----------
整条链路里**只有两步联网付费**：

    stage 0  正文 → main.tex（结构化，实测一篇 10482 token）
    stage 2  幻灯片 → 讲稿（VLM 要读全部幻灯片，实测占一篇 token 的 **76%**）

其余（TTS / 光标定位 / 数字人 / 合片）都是本地 GPU 计算 —— 只花时间、不花钱。

同一篇论文 + 同一组配置重复跑时，这两步的产物是完全确定的，
那就没有理由再付一次钱。于是把它们按指纹存起来：

    jobs/_cache/
        struct/<指纹>/main.tex  + manifest.json
        deck/<指纹>/slide_imgs/ + slides_rendered.pdf
                    + subtitle_w_cursor.txt + manifest.json

命中 deck 之后，本次只跑 stage 2/3 的**本地**部分：`--reuse_script True` 会让
pipeline 跳过讲稿那次 VLM 调用（它自己还会用 validate_script_output 复查一遍，
所以缓存损坏也不会被当成好讲稿用）。

⚠️ 三条规矩（与 docs/内容寻址与去重方案.md 一致）
----------------------------------------------
1. **指纹必须覆盖一切影响输出的输入，且只覆盖它们。**
   少一个字段 → 输入不同却误命中 → **静默产出错误内容**（"数字人跳过判据不含
   音频"那次就是这个病）；多一个字段 → 输入变了其实输出不变却重算 → 白花钱。
   所以两类缓存各有一份**显式字段表**，下面写死，加字段必须同时想清楚这句话。
2. **只增不删。** 缓存目录可能被多个任务引用，删除前必须先问"谁还在用"
   —— 本仓库踩过"按前缀批量删"的事故。本模块**故意不提供删除函数**，
   只提供 `plan_gc()` 做只读的清点，删除留给人工决策。
3. **口径变了必须 +1**：改了提示词、改了流程、改了模型默认值 → `CACHE_VERSION + 1`。
   忘了这步的后果是"新代码 + 旧产物"，而且不报错。
"""
import hashlib
import io
import json
import os
import shutil
import time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE_ROOT = os.path.join(REPO_ROOT, "jobs", "_cache")

CACHE_VERSION = "2"

# ── 指纹字段表（规矩 1：显式、写死）──
# struct：结构化只看「正文 + 论文类型 + 用哪个模型」。刻意**不含**人像/音色/语言
# —— 那些不影响 main.tex，含进来会让"只换个头像"也白花一次结构化的钱。
STRUCT_FIELDS = ("v", "source_sha1", "paper_type", "llm_model", "struct_mode")
# deck：幻灯片与讲稿看「main.tex + 两种语言 + 论文类型 + 两个模型 + 数字人档位
# + 是否开树搜索 + 工程里有哪些图」。同样刻意**不含**人像/音色：
# 换头像换声音不影响这几页幻灯片与讲稿。
#
# `if_tree_search`（幻灯片布局微调）**必须在表里**：它改变 stage 1 的幻灯片产物
# （每张溢出页生成 4 个候选、由 VLM 选一张，即产物内容真的不同），
# 漏掉它就会出现"关着开关产出的幻灯片被开着开关的任务命中"这种静默错配。
# ⚠️ 值的**归一化**由调用方负责（app._artifact_facts 统一成 bool）：
#    否则 True / "True" / 1 会算出三个不同的键 —— 行为一样却互相不命中，
#    表现是"缓存时灵时不灵"，比彻底不命中更难查。
#
# `figures`（论文配图清单指纹，2026-09-19 加）**也必须在表里**：幻灯片里能不能
# 出现 `\includegraphics` 完全取决于"工程里有哪些图"。上游入口是带图的 LaTeX
# 工程，而我们的前端从 PDF 提取（scripts/paper_figures.py）—— 哪天提取逻辑升级、
# 或者同一篇论文先跑于"还没接提图"的版本，漏掉这个字段就会命中"没有图的那一版"，
# 表现为"幻灯片里怎么还是没有图"，且不报错。
DECK_FIELDS = ("v", "main_tex_sha1", "slide_lang", "script_lang",
               "slide_prompt_sha1", "script_prompt_sha1", "slide_count", "paper_type",
               "llm_model", "vlm_model", "talking_profile", "if_tree_search",
               "figures", "course_plan_sha")

# deck 缓存里复用什么（前两项是 stage 2 读幻灯片的输入，第三项是讲稿）
DECK_SLIDES_DIR = "slide_imgs"
DECK_PDF = "slides_rendered.pdf"
DECK_SCRIPT = "subtitle_w_cursor.txt"

# ── 「输入必须可辨认」的必需字段（缺了就不许用缓存）──
# ⚠️ 这条是 2026-09-19 写测试时发现的真问题：指纹里取不到的字段会算成 None，
#    于是**两个输入完全不同、但都取不到指纹的任务会塌成同一个键** ——
#    后跑的那个直接命中前一个的产物，而它们可能根本是两篇不同的论文。
#    代价是"取不到指纹时退化成不用缓存"（多花一次钱），
#    换来的是一定不会拿错产物 —— 与阶段 2「任何不确定都判为不能复用」同一条原则。
REQUIRED_FIELDS = {"struct": ("source_sha1",), "deck": ("main_tex_sha1",)}


def _unidentified(kind, facts):
    """输入不可辨认时返回原因字符串，否则返回 None。"""
    missing = [f for f in REQUIRED_FIELDS.get(kind, ()) if not (facts or {}).get(f)]
    if missing:
        return "缺少{}（无法确定输入是谁，不使用缓存）".format("、".join(missing))
    return None


# ──────────────────────────────────────────────────────────────
# 指纹
# ──────────────────────────────────────────────────────────────
def digest_file(file_path):
    """文件内容 sha1。读不到返回 None —— 调用方据此判为"不确定"，别当空串。"""
    if not file_path or not os.path.isfile(file_path):
        return None
    h = hashlib.sha1()
    with io.open(file_path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def digest_text(text):
    return hashlib.sha1((text or "").encode("utf-8")).hexdigest()


def _key(facts, fields):
    """按字段表算指纹。

    用 sort_keys 让字段顺序不影响结果；**缺失字段按 None 参与** ——
    缺字段应当是可见的差异，而不是与被填上的那版静默相等。
    """
    payload = {f: (facts or {}).get(f) for f in fields}
    payload["v"] = CACHE_VERSION
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha1(blob).hexdigest()[:12]


def struct_key(facts):
    return _key(facts, STRUCT_FIELDS)


def deck_key(facts):
    return _key(facts, DECK_FIELDS)


def cache_dir(kind, key):
    return os.path.join(CACHE_ROOT, kind, key)


# ──────────────────────────────────────────────────────────────
# manifest：让"这条缓存是拿什么算出来的"可查
# ──────────────────────────────────────────────────────────────
def read_manifest(directory):
    p = os.path.join(directory or "", "manifest.json")
    if not os.path.isfile(p):
        return None
    try:
        with io.open(p, encoding="utf-8") as handle:
            return json.load(handle)
    except Exception:
        return None


def _write_manifest(directory, data):
    with io.open(os.path.join(directory, "manifest.json"), "w",
                 encoding="utf-8", newline="") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)


def _now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


# ──────────────────────────────────────────────────────────────
# struct 缓存：正文 + 论文类型 → main.tex
# ──────────────────────────────────────────────────────────────
def lookup_struct(facts, key=None):
    """命中返回 ``(main.tex 路径, 缓存目录, 说明)``；否则第一项为 None。

    `key` 可由调用方传入（查询与写入用同一个键，见 publish_struct 的说明）。
    """
    why_bad = _unidentified("struct", facts)
    if why_bad:
        return None, cache_dir("struct", key or struct_key(facts)), why_bad
    directory = cache_dir("struct", key or struct_key(facts))
    main_tex = os.path.join(directory, "main.tex")
    if os.path.isfile(main_tex):
        man = read_manifest(directory) or {}
        return main_tex, directory, "同名正文与论文类型已结构化过（%s）" % (
            man.get("created_at") or "时间未知")
    return None, directory, "没有这条指纹的缓存"


def publish_struct(facts, main_tex_path, job_id=None, key=None):
    """把 main.tex 存进缓存。失败只提示 —— **缓存失败不该影响出片**。

    ⚠️ `key` 必须由调用方把**查询时用的那个键**传进来，别在这里重算：
    重算一旦与查询时的输入不同（例如流水线中途改写了 main.tex），
    就会出现"写入键 ≠ 查询键"，缓存**永远命中不了而且不报错**。
    """
    if not os.path.isfile(main_tex_path):
        return None
    why_bad = _unidentified("struct", facts)
    if why_bad:
        print("[cache] 提示：不写入结构化缓存 —— {}".format(why_bad))
        return None
    key = key or struct_key(facts)
    directory = cache_dir("struct", key)
    try:
        os.makedirs(directory, exist_ok=True)
        shutil.copy2(main_tex_path, os.path.join(directory, "main.tex"))
        _write_manifest(directory, {
            "kind": "struct", "key": key,
            "fields": list(STRUCT_FIELDS),
            "facts": {f: (facts or {}).get(f) for f in STRUCT_FIELDS},
            "saved": {"main.tex": os.path.getsize(main_tex_path)},
            "from_job": job_id, "created_at": _now(),
            # 存的是**绝对路径的快照**，只为追溯；产物本身是本目录里的 main.tex
            "source": os.path.abspath(main_tex_path),
        })
    except Exception as exc:
        print("[cache] 提示：结构化缓存写入失败（{}），下次会重新生成"
              .format(type(exc).__name__))
        return None
    return directory


# ──────────────────────────────────────────────────────────────
# deck 缓存：main.tex + 语言 → 幻灯片 + 讲稿
# ──────────────────────────────────────────────────────────────
def lookup_deck(facts, key=None):
    """命中返回 ``(条目, 缓存目录, 说明)``；条目里 slides / script 分开报。

    分开报是有用的：只有幻灯片时仍值得复用（省掉 stage 1 那次调用），
    讲稿照常重新生成 —— 那是"换了个讲稿语言"的常见情形。
    """
    why_bad = _unidentified("deck", facts)
    if why_bad:
        return None, cache_dir("deck", key or deck_key(facts)), why_bad
    directory = cache_dir("deck", key or deck_key(facts))
    slides = (os.path.isdir(os.path.join(directory, DECK_SLIDES_DIR))
              and os.path.isfile(os.path.join(directory, DECK_PDF)))
    script = os.path.isfile(os.path.join(directory, DECK_SCRIPT))
    if not slides and not script:
        return None, directory, "没有这条指纹的缓存"
    man = read_manifest(directory) or {}
    entry = {"dir": directory, "slides": bool(slides), "script": bool(script),
             "manifest": man}
    return entry, directory, "幻灯片=%s / 讲稿=%s（%s）" % (
        "有" if slides else "无", "有" if script else "无",
        man.get("created_at") or "时间未知")


def publish_deck(facts, result_dir, job_id=None, key=None):
    """把产物目录里的幻灯片与讲稿存进缓存；缺哪个就只存哪个。

    ⚠️ 同 `publish_struct`：`key` 由调用方给（查询用的那个），**不要在这里重算**。
    """
    why_bad = _unidentified("deck", facts)
    if why_bad:
        print("[cache] 提示：不写入幻灯片/讲稿缓存 —— {}".format(why_bad))
        return None
    key = key or deck_key(facts)
    directory = cache_dir("deck", key)
    saved = {}
    try:
        src_slides = os.path.join(result_dir, DECK_SLIDES_DIR)
        if os.path.isdir(src_slides):
            names = [n for n in os.listdir(src_slides) if n.lower().endswith(".png")]
            if names:
                dst = os.path.join(directory, DECK_SLIDES_DIR)
                os.makedirs(dst, exist_ok=True)
                for n in names:
                    # 用 copy 而不是硬链接：硬链接共享 inode，将来就地改写
                    # （重跑 stage 1 会覆盖 slide_imgs/*.png）会把缓存一起改掉。
                    shutil.copy2(os.path.join(src_slides, n), os.path.join(dst, n))
                saved[DECK_SLIDES_DIR] = len(names)
        for name in (DECK_PDF, DECK_SCRIPT):
            src = os.path.join(result_dir, name)
            if os.path.isfile(src):
                os.makedirs(directory, exist_ok=True)
                shutil.copy2(src, os.path.join(directory, name))
                saved[name] = os.path.getsize(src)
        if not saved:
            return None
        _write_manifest(directory, {
            "kind": "deck", "key": key,
            "fields": list(DECK_FIELDS),
            "facts": {f: (facts or {}).get(f) for f in DECK_FIELDS},
            "saved": saved, "from_job": job_id, "created_at": _now(),
            "source_result_dir": os.path.abspath(result_dir),
        })
    except Exception as exc:
        print("[cache] 提示：幻灯片/讲稿缓存写入失败（{}），下次会重新生成"
              .format(type(exc).__name__))
        return None
    return directory


def reuse_into(cache_directory, result_dir, entry):
    """把缓存里的幻灯片（与讲稿，若有）放进本次的产物目录。返回拷了哪些。"""
    os.makedirs(result_dir, exist_ok=True)
    copied = []
    if entry.get("slides"):
        src = os.path.join(cache_directory, DECK_SLIDES_DIR)
        dst = os.path.join(result_dir, DECK_SLIDES_DIR)
        os.makedirs(dst, exist_ok=True)
        for n in sorted(os.listdir(src)):
            if n.lower().endswith(".png"):
                shutil.copy2(os.path.join(src, n), os.path.join(dst, n))
                copied.append("%s/%s" % (DECK_SLIDES_DIR, n))
        for name in (DECK_PDF,):
            p = os.path.join(cache_directory, name)
            if os.path.isfile(p):
                shutil.copy2(p, os.path.join(result_dir, name))
                copied.append(name)
    if entry.get("script"):
        p = os.path.join(cache_directory, DECK_SCRIPT)
        if os.path.isfile(p):
            shutil.copy2(p, os.path.join(result_dir, DECK_SCRIPT))
            copied.append(DECK_SCRIPT)
    return copied


# ──────────────────────────────────────────────────────────────
# 只读清点（供 /api/cache 与人工决策；本模块不提供删除）
# ──────────────────────────────────────────────────────────────
def _entry_size(directory):
    total = 0
    for root, _dirs, files in os.walk(directory):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def status():
    """缓存现状：条目数、占用、最近写入时间。"""
    out = {"root": CACHE_ROOT, "version": CACHE_VERSION, "kinds": {}}
    for kind in ("struct", "deck"):
        base = os.path.join(CACHE_ROOT, kind)
        rows = []
        if os.path.isdir(base):
            for name in sorted(os.listdir(base)):
                d = os.path.join(base, name)
                if not os.path.isdir(d):
                    continue
                man = read_manifest(d) or {}
                rows.append({"key": name, "size_mb": round(_entry_size(d) / 2 ** 20, 1),
                             "created_at": man.get("created_at"),
                             "from_job": man.get("from_job"),
                             "saved": sorted((man.get("saved") or {}).keys())})
        out["kinds"][kind] = {
            "count": len(rows),
            "total_mb": round(sum(r["size_mb"] for r in rows), 1),
            "entries": sorted(rows, key=lambda r: r.get("created_at") or "", reverse=True),
        }
    return out


def referenced_keys(jobs_root):
    """扫所有任务记录，收集它们**用到过**的缓存指纹。

    任务记录里的字段是 `cache.struct_key` / `cache.deck_key`（app.py 写入）——
    判据是"谁还在用"，不是"目录名长什么样"。
    """
    used = {"struct": set(), "deck": set()}
    if not os.path.isdir(jobs_root):
        return used
    for name in os.listdir(jobs_root):
        p = os.path.join(jobs_root, name, "job.json")
        if not os.path.isfile(p):
            continue
        try:
            with io.open(p, encoding="utf-8") as handle:
                job = json.load(handle)
        except Exception:
            continue
        info = job.get("cache") or {}
        for kind in ("struct", "deck"):
            key = info.get(kind + "_key")
            if isinstance(key, str) and key:
                used[kind].add(key)
    return used


def plan_gc(jobs_root):
    """只读地列出「无人引用」的缓存条目。**不删除** —— 见模块 docstring 规矩 2。"""
    used = referenced_keys(jobs_root)
    plan = {"referenced": {}, "unreferenced": {}}
    for kind in ("struct", "deck"):
        base = os.path.join(CACHE_ROOT, kind)
        names = sorted(os.listdir(base)) if os.path.isdir(base) else []
        plan["referenced"][kind] = [n for n in names if n in used[kind]]
        plan["unreferenced"][kind] = [
            {"key": n, "size_mb": round(_entry_size(os.path.join(base, n)) / 2 ** 20, 1)}
            for n in names if n not in used[kind]]
    return plan
