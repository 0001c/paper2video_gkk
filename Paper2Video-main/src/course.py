"""课程计划、讲稿分批与分段指纹。纯标准库，可独立验证，不加载模型。"""
import hashlib
import json
import math
import re
from pathlib import Path

VERSION = 2
SCRIPT_GENERATION_VERSION = 10


def digest(value):
    blob = value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def write_json(filename, value):
    write_text(filename, json.dumps(value, ensure_ascii=False, indent=2))


def write_text(filename, value):
    filename = Path(filename)
    filename.parent.mkdir(parents=True, exist_ok=True)
    temporary = filename.with_suffix(filename.suffix + ".tmp")
    temporary.write_text(value, encoding="utf-8", newline="")
    temporary.replace(filename)


def read_json(filename, default=None):
    try:
        return json.loads(Path(filename).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def save_script(filename, text):
    filename = Path(filename)
    if filename.is_file():
        if filename.read_text(encoding='utf-8') == text:
            return
        old = filename.read_bytes()
        backup = filename.parent / 'script_versions' / (digest(old) + '.txt')
        if not backup.is_file():
            write_text(backup, old.decode('utf-8'))
    write_text(filename, text)


def latex_source(root):
    """读取工程内的普通 input/include，不执行 TeX，不允许引用工程外文件。"""
    root = Path(root).resolve()
    budget = 0
    references = re.compile(r"\\(?:input|include)\s*(?:\{([^}]+)\}|([^\s{}]+))")
    def read(path, active):
        nonlocal budget
        path = path.resolve()
        if not path.is_relative_to(root) or path in active or len(active) >= 16:
            raise ValueError('section_ids：LaTeX 引用越界、循环或嵌套过深，请上传选定小节的 PDF/Word')
        if not path.is_file() or path.stat().st_size > 2000000:
            raise ValueError('section_ids：LaTeX 子文件缺失或过大，请上传完整工程或选定小节的 PDF/Word')
        text = path.read_text(encoding='utf-8')
        budget += len(text)
        if budget > 2000000:
            raise ValueError('section_ids：LaTeX 工程展开超过 2000000 字符，请拆分素材')
        text = re.sub(r'(?<!\\)%[^\n]*', '', text)
        def expand(match):
            name = (match.group(1) or match.group(2)).strip()
            child = path.parent / name
            if not child.suffix:
                child = child.with_suffix('.tex')
            return '\n' + read(child, [*active, path]) + '\n'
        return references.sub(expand, text)
    return read(root / 'main.tex', [])


def source_sections(text):
    """仅识别有明确标记的标题；无标题时提供完整材料，不猜测分页范围。"""
    matches = list(re.finditer(r"(?m)^(?:#{1,6}\s+(.+)|\\(?:chapter|(?:sub)*section)\*?\{([^}]+)\}|((?:第[一二三四五六七八九十百\d]+[章节]|\d+\.\d+(?:\.\d+)?)\s+[^\n]{1,100}))\s*$", text))
    result = [{"id": "all", "title": "全部材料", "start": 0, "end": len(text), "chars": len(text)}]
    def level(match):
        raw = match.group(0)
        if raw.startswith('#'):
            return len(raw) - len(raw.lstrip('#'))
        if raw.startswith('\\'):
            return 0 if raw.startswith('\\chapter') else raw.count('sub') + 1
        return raw.split()[0].count('.') + 1
    for i, match in enumerate(matches[:200]):
        end = next((m.start() for m in matches[i + 1:] if level(m) <= level(match)), len(text))
        title = next(g for g in match.groups() if g).strip()
        result.append({"id": str(match.start()), "title": title[:120], "start": match.start(), "end": end, "chars": end - match.start()})
    return result


def selected_source(text, ids):
    choices = {s["id"]: s for s in source_sections(text)}
    if not isinstance(ids, list) or not ids or len(ids) > 40 or any(not isinstance(i, str) or i not in choices for i in ids):
        raise ValueError("section_ids：请选择有效的章节，最多 40 项")
    if len(set(ids)) != len(ids) or ("all" in ids and len(ids) > 1):
        raise ValueError("section_ids：全部材料不能与其它章节同时选择，章节不能重复")
    sections = sorted((choices[i] for i in ids), key=lambda s: s["start"])
    if any(a['end'] > b['start'] for a, b in zip(sections, sections[1:])):
        raise ValueError('section_ids：选定章节存在包含关系，请仅选择章或其中的小节')
    return sections, "\n\n".join(text[s["start"]:s["end"]] for s in sections)


def number(value, name, low, high):
    if isinstance(value, bool):
        raise ValueError(name + "：必须是数字")
    try:
        value = float(value)
    except (ValueError, TypeError):
        raise ValueError(name + "：必须是数字")
    if not math.isfinite(value) or not low <= value <= high:
        raise ValueError("{}：范围 {}～{}".format(name, low, high))
    return value


def build_plan(text, settings, page_count, script_lang):
    allowed = {"section_ids", "duration_mode", "target_minutes", "speech_rate", "audience", "objectives", "focus", "depth", "sections"}
    if not isinstance(settings, dict) or settings.keys() - allowed:
        raise ValueError("course：包含未知字段或格式错误")
    selected, source = selected_source(text, settings.get("section_ids", ["all"]))
    if not source.strip() or len(source) > 150000:
        raise ValueError("section_ids：材料为空或超过 150000 字符，请选择更小的教学范围")
    rate = number(settings.get("speech_rate", 220 if script_lang == "zh" else 140), "speech_rate", 80, 400)
    mode = settings.get("duration_mode", "auto")
    if mode not in ("auto", "custom"):
        raise ValueError("duration_mode：必须是 auto 或 custom")
    target = number(settings.get("target_minutes"), "target_minutes", 1, 180) if mode == "custom" else None
    page_count = int(number(page_count, "slide_count", 4, 40))
    if len(selected) > page_count:
        raise ValueError("slide_count：页数应不少于所选小节数量")
    plan = {"version": VERSION, "duration_mode": mode, "target_minutes": target, "speech_rate": rate,
            "script_lang": script_lang, "slide_count": page_count, "section_ids": [s["id"] for s in selected],
            "source_sha": digest(source), "sections": []}
    for key in ("audience", "objectives", "focus", "depth"):
        value = settings.get(key, "")
        if not isinstance(value, str) or len(value) > 2000:
            raise ValueError(key + "：必须为不超过 2000 字符的文本")
        plan[key] = value.strip()
    total = target or max(len(selected), min(60, round(page_count * 0.8, 1)))
    weights = [max(1, s["chars"]) for s in selected]
    allocated = [1] * len(selected)
    for _ in range(page_count - len(selected)):
        idx = max(range(len(selected)), key=lambda i: weights[i] / allocated[i])
        allocated[idx] += 1
    custom = settings.get("sections")
    if custom is not None and (not isinstance(custom, list) or len(custom) != len(selected)):
        raise ValueError("sections：时间分配项必须与所选小节一一对应")
    first_page = 1
    for i, s in enumerate(selected):
        minutes = total * weights[i] / sum(weights)
        title = s["title"]
        if custom is not None:
            item = custom[i]
            if not isinstance(item, dict) or set(item) != {"title", "minutes"}:
                raise ValueError("sections：每项必须包含 title 和 minutes")
            title = item["title"]
            if not isinstance(title, str) or not title.strip() or len(title) > 120:
                raise ValueError("sections.title：必须为 1～120 字符")
            minutes = number(item["minutes"], "sections.minutes", 0.1, 180)
        plan["sections"].append({"id": s["id"], "title": title, "minutes": round(minutes, 3),
                                  "first_page": first_page, "last_page": first_page + allocated[i] - 1,
                                  "source": text[s["start"]:s["end"]]})
        first_page += allocated[i]
    actual = sum(s["minutes"] for s in plan["sections"])
    if target is not None and abs(actual - target) > 0.02:
        raise ValueError("sections.minutes：分节时间之和必须等于目标时长")
    if actual > 180:
        raise ValueError("sections.minutes：总时长不能超过 180 分钟")
    if actual * rate / page_count > 3000:
        raise ValueError("slide_count：每页预算超过 3000 字/词，请增加页数或缩短时长")
    plan["planned_minutes"] = round(actual, 2)
    from teaching_quality import source_examples
    plan['examples'] = source_examples(source)
    plan["warnings"] = []
    if actual * rate > len(source) * 4:
        plan["warnings"].append("目标讲稿量明显多于材料，请补充例题、推导或缩短时长；不要通过重复内容填满时间。")
    plan["token"] = digest(plan)
    return plan, source


def public_plan(plan):
    return {**plan, "sections": [{k: v for k, v in s.items() if k != "source"} for s in plan["sections"]]}


def page_sections(plan, count, topics=None):
    """实际课件页数可能偏离目标，按已审核的时间比例映射，保持顺序与完整覆盖。"""
    allocations = [1] * len(plan["sections"])
    if count < len(allocations):
        raise ValueError("实际课件页数少于教学小节数，请修改教案后重新生成")
    for _ in range(count - len(allocations)):
        i = max(range(len(allocations)), key=lambda i: plan["sections"][i]["minutes"] / allocations[i])
        allocations[i] += 1
    result = []
    for s, n in zip(plan["sections"], allocations):
        result.extend([{**s, "page_minutes": s["minutes"] / n}] * n)
    if topics is not None and len(topics) == count:
        from lesson_planning import allocate_minutes, page_tasks
        return allocate_minutes(result,page_tasks(topics,plan.get('examples',[])))
    return result


def slide_topics(result_dir, count):
    topics = ['第 {} 页'.format(i+1) for i in range(count)]
    pdf = Path(result_dir)/'slides_rendered.pdf'
    if pdf.is_file():
        import pymupdf
        with pymupdf.open(pdf) as document:
            if len(document) == count:
                topics = [page.get_text()[:1600] for page in document]
    return topics


def split_pages(script):
    return [p.strip() for p in re.split(r"(?m)^\s*###\s*$", script) if p.strip()]


def teaching_review(pages, result_dir, policy):
    if not policy:
        return None
    from teaching_quality import script_report
    topics = slide_topics(result_dir,len(pages))
    report = script_report(pages,timing_policy(policy,result_dir),topics)
    report['revision_stops'] = []
    for i, page in enumerate(pages):
        cached = read_json(Path(result_dir)/'course_script'/f'{i}.json',{})
        if cached.get('text')==page and cached.get('needs_review'):
            reason = cached.get('stop_reason','repair_limit')
            report['revision_stops'].append({'page':i+1,'reason':reason,'attempts':len(cached.get('history',[]))})
            label = {'no_oral_change':'修订未改变口播','repeated_oral_content':'修订回到了已有口播','repair_limit':'已用完修订额度'}.get(reason,reason)
            report['warnings'].append(f'第{i+1}页{label}，已停止自动请求，请按问题清单审核')
    for i in range(1,len(pages)):
        cached = read_json(Path(result_dir)/'course_script'/f'{i}.json',{})
        prefix = digest([spoken_text(page) for page in pages[:i]])
        if cached.get('context_sha') and cached['context_sha']!=prefix:
            report['warnings'].append('第{}页生成后前文有变动，请复核其承接、重复讲解和跨页问题；必要时重写本页'.format(i+1))
    return report


def spoken_text(page):
    from script_validation import parse_script
    return "\n\n\n".join(speech for lines in parse_script(page) for speech, cursor in lines)


def estimate_minutes(page, language, rate):
    from spoken_text import prepare_spoken
    text = spoken_text(page)
    try:
        text = prepare_spoken(text,language)[0]
    except ValueError:
        # 预算只是估算；审核页必须仍可打开修正，TTS 的整章预检负责禁止收费。
        pass
    units = len(re.findall(r"[\u3400-\u9fff]", text)) + len(re.findall(r"[A-Za-z0-9]+", text))
    return units / rate


def slide_directive(plan):
    outline = "\n".join("- {}：约 {:.1f} 分钟，第 {}～{} 页".format(s["title"], s["minutes"], s["first_page"], s["last_page"]) for s in plan["sections"])
    from teaching_quality import directive
    return ("\n【已审核的课程教案；内部教学控制，不是页面文案】\n受众背景（仅调节知识起点）：{audience}\n学习目标：{objectives}\n重点难点：{focus}\n讲解深度：{depth}\n"
            "按下列顺序覆盖所选材料，包含学习目标、概念解释、源材料例题/分步推导、易错点与小结。每页服务于教学，封面和小结计入上述页数。"
            "不得虚构源材料的例题答案或研究事实，源材料不足时明确说明，不要重复填充。\n内部覆盖顺序与节奏（不要显示分钟预算）：\n{outline}").format(outline=outline, **plan) + directive(plan.get('examples',[]))


def generate_script(plan, slide_dir, prompt_file, model_config, output_dir, generator, selected=None, workers=3, generation_id=None, teaching=None):
    from course_script import generate_script as generate
    return generate(timing_policy(plan,output_dir),slide_dir,prompt_file,model_config,output_dir,generator,
                    selected,workers,generation_id,timing_policy(teaching,output_dir))


def _audio_voice(manifest):
    if not isinstance(manifest,dict) or not manifest or any(not isinstance(p,dict) or not isinstance(p.get('voice_identity'),str) or not p['voice_identity'] for p in manifest.values()):
        return None
    voices = {p['voice_identity'] for p in manifest.values()}
    return next(iter(voices)) if len(voices)==1 else None


def timing_policy(policy, result_dir):
    """仅借用当前任务、同音色参数、同预算的已完成配音测量，不修改用户教案。"""
    if not policy or not policy.get('target_minutes'):
        return policy
    from spoken_text import NORMALIZATION_VERSION
    calibrated = read_json(Path(result_dir)/'timing_calibration.json',{})
    calibrated = calibrated if isinstance(calibrated,dict) else {}
    measured = calibrated.get('measured_speech_rate')
    voice = _audio_voice(read_json(Path(result_dir)/'audio/manifest.json',{}))
    configured = policy.get('_configured_speech_rate',policy['speech_rate'])
    if (voice and calibrated.get('normalization_version')==NORMALIZATION_VERSION
            and calibrated.get('voice_identity')==voice and calibrated.get('configured_speech_rate')==configured
            and calibrated.get('plan_token')==policy.get('token') and calibrated.get('target_minutes')==policy['target_minutes']
            and type(measured) in (int,float) and 80<=measured<=400):
        return {**policy,'speech_rate':calibrated['measured_speech_rate'],'_configured_speech_rate':configured}
    return policy


def audio_report(plan, result_dir):
    plan = timing_policy(plan,result_dir)
    manifest = read_json(Path(result_dir) / "audio" / "manifest.json", {})
    manifest = manifest if isinstance(manifest,dict) else {}
    script_path = Path(result_dir) / "subtitle_w_cursor.txt"
    pages = split_pages(script_path.read_text(encoding="utf-8")) if script_path.is_file() else []
    sections = page_sections(plan,len(pages),slide_topics(result_dir,len(pages))) if pages else []
    items = []
    for i, page in enumerate(pages):
        cached = manifest.get(str(i), {})
        cached = cached if isinstance(cached,dict) else {}
        current = cached.get("text_sha") == digest(spoken_text(page))
        audio = Path(result_dir)/'audio'/f'{i}.wav'
        seconds = None
        if current and audio.is_file() and cached.get('wav_sha') == digest(audio.read_bytes()):
            import soundfile as sf
            try:
                info = sf.info(str(audio))
                duration = info.frames/info.samplerate
                if math.isfinite(duration) and duration>0:
                    seconds = duration
            except (OSError,RuntimeError,ValueError):
                pass
        items.append({"page": i + 1, "section": sections[i]["title"], "planned_minutes": sections[i]["page_minutes"],
                      "estimated_minutes": round(estimate_minutes(page, plan["script_lang"], plan["speech_rate"]), 2),
                      "actual_seconds": seconds,
                      'actual_within_budget':abs(seconds/60-sections[i]['page_minutes'])<=sections[i]['page_minutes']*.1 if seconds is not None else None})
    actual = sum(p["actual_seconds"] or 0 for p in items)
    complete = bool(items) and all(p["actual_seconds"] is not None for p in items)
    target = plan.get('target_minutes')
    passed = abs(actual/60-target)<=target*.1+1e-9 if complete and target else None
    issues = []
    if target and not complete:
        issues.append('音频尚未完整生成或与当前讲稿不匹配，请先完成配音')
    elif target and not passed:
        issues.append('实际音频{:.2f}分钟，目标{}分钟，超出±10%；请调整讲稿后仅重做变化页的配音'.format(actual/60,target))
    return {'version':2,"pages": items, "actual_minutes": round(actual / 60, 2) if complete else None,
            "estimated_minutes": round(sum(p["estimated_minutes"] for p in items), 2),
            "target_minutes": target, "tolerance": 0.1,'actual_passed':passed,'issues':issues,
            'configured_speech_rate':plan.get('_configured_speech_rate',plan['speech_rate']),
            'estimate_speech_rate':plan['speech_rate'],
            'measured_speech_rate':round(sum(estimate_minutes(page,plan['script_lang'],1) for page in pages)/(actual/60),1) if complete else None}


def require_audio_timing(plan, result_dir):
    """自定义时长在配音后、光标/数字人与合片前验收；保留已有音频供局部修订复用。"""
    report = audio_report(plan,result_dir)
    voice = _audio_voice(read_json(Path(result_dir)/'audio/manifest.json',{}))
    rate = report['measured_speech_rate']
    if voice and rate is not None and 80<=rate<=400:
        from spoken_text import NORMALIZATION_VERSION
        write_json(Path(result_dir)/'timing_calibration.json',{'voice_identity':voice,'plan_token':plan.get('token'),
            'normalization_version':NORMALIZATION_VERSION,
            'configured_speech_rate':report['configured_speech_rate'],'target_minutes':plan.get('target_minutes'),
            'measured_speech_rate':rate,'measured_minutes':report['actual_minutes']})
    write_json(Path(result_dir)/'course_timing.json',report)
    if plan.get('target_minutes') and report['actual_passed'] is not True:
        raise ValueError('音频时长未通过合片前验收：'+'；'.join(report['issues']))
    return report


def generate_selected(slide_dir, prompt_file, model_config, output_dir, generator, selected, workers=3, generation_id=None, teaching=None):
    return generate_script(None, slide_dir, prompt_file, model_config, output_dir, generator, selected, workers, generation_id, teaching)


def export_chapters(plan, result_dir):
    report = audio_report(plan, result_dir)
    write_json(Path(result_dir) / "course_timing.json", report)
    seconds, last, chapters = 0, None, []
    for p in report["pages"]:
        if p["section"] != last:
            chapters.append({"title": p["section"], "start_seconds": round(seconds, 2)})
            last = p["section"]
        seconds += p["actual_seconds"] or 0
    write_json(Path(result_dir) / "chapters.json", chapters)
    Path(result_dir, "chapters.txt").write_text("\n".join("{:02d}:{:02d}:{:02d} {}".format(int(c["start_seconds"]) // 3600, int(c["start_seconds"]) // 60 % 60, int(c["start_seconds"]) % 60, c["title"]) for c in chapters), encoding="utf-8")
    return report


def embed_chapters(plan, result_dir):
    """无重编码加入 MP4 章节，失败明确报错，保留原字幕视频。"""
    import subprocess
    chapters = read_json(Path(result_dir) / 'chapters.json', [])
    report = audio_report(plan, result_dir)
    if report['actual_minutes'] is None:
        raise ValueError('音频时长不完整，不能生成章节导航')
    end = round(report['actual_minutes'] * 60000)
    metadata = [';FFMETADATA1']
    for i, chapter in enumerate(chapters):
        title = re.sub(r'([\\=;#])', r'\\\1', chapter['title']).replace('\n', ' ')
        stop = round(chapters[i + 1]['start_seconds'] * 1000) if i + 1 < len(chapters) else end
        metadata.extend(['[CHAPTER]', 'TIMEBASE=1/1000', 'START=' + str(round(chapter['start_seconds'] * 1000)), 'END=' + str(stop), 'title=' + title])
    meta = Path(result_dir, 'chapters.ffmetadata')
    meta.write_text('\n'.join(metadata), encoding='utf-8')
    target = Path(result_dir, 'course_video.mp4')
    temporary = Path(result_dir, '.course_video.tmp.mp4')
    subprocess.run(['ffmpeg', '-y', '-v', 'error', '-i', str(Path(result_dir, '3_merage.mp4')),
                    '-f', 'ffmetadata', '-i', str(meta), '-map', '0', '-map_chapters', '1',
                    '-c', 'copy', '-movflags', '+faststart', str(temporary)], check=True)
    temporary.replace(target)
