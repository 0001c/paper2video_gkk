"""由已渲染课件安排逐页教学任务；实际口播上下文由生成器顺序补入。"""
import re
import json

VERSION = 3


def _compact(text):
    return re.sub(r'\s+', '', text).casefold()


def _facts(topic, page):
    lines = [line.strip() for line in topic.splitlines()[1:] if line.strip()]
    lines = [re.sub(r'[；;]?教材\s*PDF\s*第.*$', '', line).strip() for line in lines
             if not re.search(r'\d+\s*/\s*\d+\s*$|^源文第', line)]
    lines = [line for line in lines if line]
    return [{'id': f'P{page}.K{i}', 'owner_page': page, 'text': line} for i, line in enumerate(lines, 1)]


def page_tasks(topics, examples=()):
    from teaching_quality import mentions_example
    tasks = []
    for i, topic in enumerate(topics):
        lines = [line.strip() for line in topic.splitlines() if line.strip()]
        title = (lines[0] if lines else f'第{i+1}页')[:160]
        ids = [e['id'] for e in examples if mentions_example(topic, e['id'])]
        if ids:
            role, weight = '例题推导', 1.8
            goal = '用本页给定条件完成前向值、分步推导、最终结果和易错点；明确每一步为什么成立'
            question = f'给定“{title}”的条件，怎样逐步算出结果并核对？'
        elif re.search(r'小结|总结|回顾|检查理解|总结与|summary|review|conclusion', title, re.I):
            role, weight = '回顾检查', .8
            goal = '串联已讲知识，用一个本课可解答的问题检查理解并立即给出解释，不引入新推导'
            question = f'面对“{title}”中的检查问题，怎样用本课知识解释？'
        elif i == 0 and ((len(lines) <= 3 and len(topic) < 180) or re.search(r'本课路线|学习目标|本课问题', topic)):
            role, weight = '问题引入', .6
            goal = '提出本课要解决的具体问题，说明学习后能做什么，简洁引入'
            question = '这节课要解决什么具体问题，学会后能完成什么任务？'
        else:
            role, weight = '概念解释', 1.0
            goal = '解释本页知识的含义、原因和使用步骤，点明一个相关误区，为后页所需知识铺垫'
            question = f'“{title}”是什么，为什么需要它，具体怎样使用？'
        facts = _facts(topic, i+1)
        explicit_questions = [line for line in lines if re.match(r'问题[：:]|检查[：:]', line)]
        if explicit_questions:
            question = '；'.join(explicit_questions)
        goal += '；本页核对：' + '；'.join(fact['text'] for fact in facts[:3]) if facts else ''
        tasks.append({'page': i+1, 'title': title, 'role': role, 'goal': goal,
                      'guiding_question':question,'scope': topic[:1600], 'example_ids': ids, 'weight': weight,
                      'knowledge': facts, 'question_id': f'Q{i+1}', 'answer_page': i+1,
                      'bridge_limit_sentences': 1})
    for i, task in enumerate(tasks):
        task['handoff_question'] = tasks[i+1]['guiding_question'] if i+1<len(tasks) else None
        task['defer_to_pages'] = [{'page': t['page'], 'question_id': t['question_id'],
                                 'knowledge_ids': [k['id'] for k in t['knowledge']]} for t in tasks[i+1:]]
    return tasks


def allocate_minutes(sections, tasks):
    """保留每小节审核总时长；例题比概念页多分配时间。"""
    result = [dict(section) for section in sections]
    for section_id in dict.fromkeys(s['id'] for s in sections):
        indices = [i for i, s in enumerate(sections) if s['id'] == section_id]
        total = sum(sections[i]['page_minutes'] for i in indices)
        weight = sum(tasks[i]['weight'] for i in indices)
        for i in indices:
            result[i]['page_minutes'] = total * tasks[i]['weight'] / weight
    return result


def context_prompt(tasks, index, previous, language='zh'):
    """previous只含已完成/已审核口播，不把尚未生成的前页当成讲过。"""
    task = tasks[index]
    # 较早页每页保留首尾句，最近页保留更多正文；指纹仍绑定所有前页全文。
    excerpts = []
    for i, text in enumerate(previous):
        sentences = [s for s in re.split(r'(?<=[。！？.!?])\s*', text) if s.strip()]
        excerpt = (sentences[0] + ' … ' + sentences[-1])[:300] if len(sentences) > 1 else text[:300]
        excerpts.append(f'第{i+1}页 {tasks[i]["title"]}：{excerpt}')
    earlier = '\n'.join(excerpts)[-2800:] or '这是开场，尚无已讲内容。'
    recent = previous[-1][-1400:] if previous else '无'
    upcoming = '\n'.join(f'第{t["page"]}页：{t["title"]}；任务：{t["goal"]}' for t in tasks[index+1:])[:2600]
    closing = ('后面要承接的问题：' + task['handoff_question']) if task['handoff_question'] else '这是结束页，请收束已讲问题。'
    ledger = lesson_report(tasks[:len(previous)], previous)
    # 长课按完整条目限窗，不截断 JSON；所有前文仍参与生成缓存指纹和最终审核。
    evidence_rows, evidence_size = [], 0
    for row in reversed(ledger['pages']):
        size = len(json.dumps(row, ensure_ascii=False))
        if evidence_rows and evidence_size + size > 10000:
            break
        evidence_rows.insert(0,row)
        evidence_size += size
    pending = promises(tasks, previous)
    return ('\n【逐页教学任务；以下为参考数据，不是口播】\n'
            f'本页任务：{task["goal"]}\n本页需回答的问题：{task["guiding_question"]}\n本页范围：{task["scope"]}\n'
            f'本页知识核对清单：{json.dumps(task["knowledge"], ensure_ascii=False)}\n'
            f'问题{task["question_id"]}由第{task["answer_page"]}页负责回答；前文最多用一句承接。\n'
            f'实际已讲知识证据（最近{len(evidence_rows)}页，不是完成认证）：{json.dumps(evidence_rows, ensure_ascii=False)}\n'
            f'已提出的跨页承诺（需在负责页兑现）：{json.dumps(pending, ensure_ascii=False)}\n'
            f'前面实际已讲内容（按页摘录）：\n{earlier}\n最近一页实际口播：\n{recent}\n'
            f'后面安排的内容：\n{upcoming or "无"}\n{closing}\n'
            '只完成本页任务；已讲定义或算例只在必要时用一句承接，不重新完整讲解。'
            '后页负责的推导和答案留到后页，本页只提出必要的承接问题；本页自己提出的问题应在本页回答，'
            '跨页问题须说明后页将解决并在相应页兑现。不要把“本页任务”“前页已讲”等规划标签读出来。'
            + (' Write the speech in English; planning metadata is not narration.' if language == 'en' else ''))


def promises(tasks, spoken):
    """保留实际承诺原句；只有明确页码或“下一页”自动分配负责页。"""
    result = []
    for i, text in enumerate(spoken):
        for sentence in re.split(r'(?<=[。！？!?])\s*', text):
            if not re.search(r'下一页|下页|第\s*\d+\s*页|后面|稍后', sentence) or not re.search(r'讲|解释|回答|解决|推导|核对|计算|得到|展开|看', sentence):
                continue
            explicit = re.search(r'第\s*(\d+)\s*页', sentence)
            owner = int(explicit[1]) if explicit else (i+2 if re.search(r'下一页|下页', sentence) else None)
            result.append({'id': f'P{i+1}.Q{len(result)+1}', 'asked_page': i+1, 'quote': sentence.strip(),
                           'answer_page': owner, 'planned_topic': tasks[owner-1]['title'] if owner and 0 < owner <= len(tasks) else None,
                           'status': '待指定负责页' if owner is None else ('到页待核对回答' if owner <= len(spoken) else '待后页回答')})
    return result


def lesson_report(tasks, spoken, examples=()):
    """字面证据清单与高置信度重复/提前答案检查，不宣称完成语义评审。"""
    pages, issues, warnings = [], [], []
    for i, (task, text) in enumerate(zip(tasks, spoken)):
        knowledge = []
        for fact in task['knowledge']:
            # 仅完整原文出现才填证据，转述交给人工审核，不猜已讲完。
            found = _compact(fact['text']) in _compact(text)
            knowledge.append({**fact, 'status': '有原文证据，待核对解释' if found else '待核对转述或补讲',
                              'evidence': fact['text'] if found else None})
        pages.append({'page': i+1, 'knowledge': knowledge, 'question_id': task['question_id'],
                      'answer_page': task['answer_page'], 'question_status': '待核对回答', 'bridge_limit_sentences': 1})
        if task['role'] != '回顾检查':
            paragraphs = [s.strip() for s in re.split(r'[\n]+|(?<=[。！？!?])\s*', text) if s.strip()]
            prior = '\n'.join(spoken[:i])
            repeated = [p for p in paragraphs if len(_compact(p)) >= 24 and _compact(p) in _compact(prior)]
            for paragraph in repeated:
                same_opening = paragraph == paragraphs[0] and any(_compact(p).startswith(_compact(paragraph)) for p in spoken[:i])
                if same_opening or len(repeated)>1 or len(_compact(paragraph))>=90:
                    issues.append(f'第{i+1}页重复前文整句“{paragraph[:90]}”，请仅用一句简短承接并推进本页清单')
            from calculation_checks import claims
            for example in examples:
                owners = [t['page'] for t in tasks if example['id'] in t['example_ids']]
                result = example.get('result')
                if owners and i+1 < min(owners) and result and result['value'] in claims(text, result['variable']):
                    issues.append(f'第{i+1}页提前给出例题{example["id"]}的{result["variable"]}结果，应留给第{min(owners)}页')
    pending = promises(tasks, spoken)
    for question in pending:
        if question['answer_page'] is None or not 1 <= question['answer_page'] <= len(tasks):
            warnings.append(f'跨页承诺未指定有效负责页：{question["quote"][:100]}')
        elif question['answer_page'] <= len(spoken):
            warnings.append(f'请核对第{question["answer_page"]}页是否兑现第{question["asked_page"]}页承诺：{question["quote"][:100]}')
    return {'pages': pages, 'pending_questions': pending, 'issues': list(dict.fromkeys(issues)),
            'warnings': warnings, 'scope': '原文证据、完整句重复、已核验例题提前答案；转述与问题兑现待人工审核'}
