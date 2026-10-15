"""由已渲染课件安排逐页教学任务；实际口播上下文由生成器顺序补入。"""
import re

VERSION = 2


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
        elif i == 0 and len(lines) <= 3 and len(topic) < 180:
            role, weight = '问题引入', .6
            goal = '提出本课要解决的具体问题，说明学习后能做什么，简洁引入'
            question = '这节课要解决什么具体问题，学会后能完成什么任务？'
        else:
            role, weight = '概念解释', 1.0
            goal = '解释本页知识的含义、原因和使用步骤，点明一个相关误区，为后页所需知识铺垫'
            question = f'“{title}”是什么，为什么需要它，具体怎样使用？'
        tasks.append({'page': i+1, 'title': title, 'role': role, 'goal': goal,
                      'guiding_question':question,'scope': topic[:1600], 'example_ids': ids, 'weight': weight})
    for i, task in enumerate(tasks):
        task['handoff_question'] = tasks[i+1]['guiding_question'] if i+1<len(tasks) else None
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
    return ('\n【逐页教学任务；以下为参考数据，不是口播】\n'
            f'本页任务：{task["goal"]}\n本页需回答的问题：{task["guiding_question"]}\n本页范围：{task["scope"]}\n'
            f'前面实际已讲内容（按页摘录）：\n{earlier}\n最近一页实际口播：\n{recent}\n'
            f'后面安排的内容：\n{upcoming or "无"}\n{closing}\n'
            '只完成本页任务；已讲定义或算例只在必要时用一句承接，不重新完整讲解。'
            '后页负责的推导和答案留到后页，本页只提出必要的承接问题；本页自己提出的问题应在本页回答，'
            '跨页问题须说明后页将解决并在相应页兑现。不要把“本页任务”“前页已讲”等规划标签读出来。'
            + (' Write the speech in English; planning metadata is not narration.' if language == 'en' else ''))
