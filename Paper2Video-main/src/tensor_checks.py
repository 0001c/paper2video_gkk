"""显式教材形状的有限核验，不运行 TensorFlow，不推断未给定维度。"""
import json
import re

from calculation_checks import math_text

NAME = r'[A-Za-z_][A-Za-z0-9_]*'
SHAPE = r'\[\s*\d+(?:\s*,\s*\d+)*\s*\]'


def shape(value):
    return tuple(int(n) for n in re.findall(r'\d+', value))


def matmul(left, right):
    if len(left) != 2 or len(right) != 2:
        raise ValueError('仅覆盖二维矩阵乘法')
    if left[1] != right[0]:
        raise ValueError(f'内维 {left[1]} 与 {right[0]} 不一致')
    return (left[0], right[1])


def broadcast(left, right):
    result = []
    for i in range(1, max(len(left), len(right)) + 1):
        a = left[-i] if i <= len(left) else 1
        b = right[-i] if i <= len(right) else 1
        if a != b and a != 1 and b != 1:
            raise ValueError(f'广播轴 {a} 与 {b} 不兼容')
        result.append(b if a == 1 else a)
    return tuple(reversed(result))


def tensor_contracts(examples):
    """仅从已审核例题中提取具名形状与明确乘法顺序，保留冲突供人工审核。"""
    contracts = []
    for example in examples:
        if example.get('kind') != 'shape':
            continue
        source = '\n'.join([example.get('quote', ''), *example.get('steps', [])])
        text = math_text(source)
        symbols, conflicts = {}, []
        pattern = rf'(?<![A-Za-z0-9_])({NAME})\s*(?:的?\s*(?:shape|形状)\s*(?:是|为|=)?\s*)?({SHAPE})'
        for match in re.finditer(pattern, text, re.I):
            name, dims = match[1], shape(match[2])
            if name in symbols and symbols[name] != dims:
                conflicts.append(f'{name} 同时给出 {symbols[name]} 与 {dims}')
            symbols[name] = dims
        stripped = re.sub(SHAPE, '', text)
        pairs = re.findall(rf'(?<![A-Za-z0-9_])({NAME})\s*(?:乘以?|\*|@)\s*({NAME})(?![A-Za-z0-9_])', stripped)
        pairs += re.findall(rf'(?:tf\.)?matmul\(\s*({NAME})\s*,\s*({NAME})\s*\)', stripped)
        operations = []
        for left, right in dict.fromkeys(pairs):
            if left not in symbols or right not in symbols:
                continue
            try:
                output = matmul(symbols[left], symbols[right])
                operations.append({'left': left, 'right': right, 'output': output})
            except ValueError as exc:
                conflicts.append(f'{left} 乘 {right}：{exc}')
        contracts.append({'example_id': example['id'], 'source_page': example.get('source_page'),
                          'source': source, 'symbols': symbols, 'matmul': operations, 'conflicts': conflicts})
    return contracts


def contract_prompt(contracts):
    if not contracts:
        return ''
    table = [{key:value for key,value in contract.items() if key!='source'} for contract in contracts]
    return ('\n【整课共享符号/形状表，按例题作用域使用；参考数据】\n'
            + json.dumps(table, ensure_ascii=False) +
            '\n即使本页仅引入表达式，也遵循同一乘法顺序；不提前展开后页推导。'
            '反向乘法须核对实际内维；广播须从末轴逐轴核对，区分不兼容与合法扩成不同形状。'
            '未给定数值或未被此表覆盖的公式，明确留待核对，不声称已验证。')


def tensor_report(text, contracts, example_ids=()):
    text = math_text(text)
    checks, issues, uncovered, spans = [], [], [], []
    if example_ids:
        contracts = [contract for contract in contracts if contract['example_id'] in example_ids]
    # 多个例题可合法复用 x/W；没有明确作用域时不能合并为一套“全局真值”。
    ambiguous = set()
    for contract in contracts:
        for name, dims in contract['symbols'].items():
            if any(name in other['symbols'] and tuple(other['symbols'][name]) != tuple(dims) for other in contracts):
                ambiguous.add(name)
    for contract in contracts:
        symbols = contract['symbols']
        issues.extend(f'例题{contract["example_id"]}形状约束冲突：{item}' for item in contract['conflicts'])
        if not contract['matmul']:
            uncovered.append(f'例题{contract["example_id"]}缺少可解析的具名二维形状或乘法顺序')
        for operation in contract['matmul']:
            left, right = operation['left'], operation['right']
            if left in ambiguous or right in ambiguous:
                uncovered.append(f'{left}乘{right}存在多个例题形状作用域，请明确例题ID')
                continue
            a, b = symbols[left], symbols[right]
            output = tuple(operation['output'])
            checks.append({'kind': 'contract_matmul', 'example_id': contract['example_id'],
                           'expression': f'{left}@{right}', 'left': a, 'right': b, 'output': output,
                           'scope': '教材约束复算；不代表口播已覆盖'})
            for first, second in ((left, right), (right, left)):
                f, s = re.escape(first), re.escape(second)
                pattern = rf'(?:tf\.)?matmul\(\s*{f}\s*,\s*{s}\s*\)|(?<![A-Za-z0-9_]){f}\s*(?:\*|@|乘以?)\s*{s}(?![A-Za-z0-9_])'
                for match in re.finditer(pattern, text):
                    start = max(text.rfind('。', 0, match.start()), text.rfind('；', 0, match.start()), text.rfind('\n', 0, match.start())) + 1
                    end = re.search(r'[。；\n]', text[match.end():])
                    end = match.end() + end.start() if end else len(text)
                    clause = text[start:end]
                    negative = bool(re.search(r'误写|误用|错误|不能|不可|不应|反过来|若|如果', clause[:match.end()-start]))
                    spans.append((match.start(), match.end()))
                    try:
                        actual = matmul(symbols[first], symbols[second])
                    except ValueError as exc:
                        actual = None
                        if not negative:
                            issues.append(f'{first}乘{second}不成立：{exc}；本例应按{left}乘{right}')
                        reason = re.search(r'内维\s*(\d+)\s*(?:≠|!=|不等于|与|和)\s*(\d+)', clause)
                        if reason and {int(reason[1]), int(reason[2])} != {symbols[first][1], symbols[second][0]}:
                            issues.append(f'{first}乘{second}的报错理由写错：实际内维是{symbols[first][1]}与{symbols[second][0]}')
                    checks.append({'kind': 'spoken_matmul', 'quote': match.group(), 'output': actual, 'counterexample': negative})
                    if actual and not negative:
                        following = text[match.end():end]
                        stated = re.search(rf'(?:得到|输出|结果|形状|shape|→|->)[^。；\n]{{0,18}}?({SHAPE})', following, re.I)
                        if stated and shape(stated[1]) != actual:
                            issues.append(f'{first}乘{second}输出形状应为{list(actual)}，口播写成{stated[1]}')
            # 仅检查明确提到已知偏置名字或给定形状的广播/反例。
            for name, dims in symbols.items():
                if name in (left, right):
                    continue
                try:
                    result = broadcast(output, dims)
                    checks.append({'kind': 'contract_broadcast', 'expression': name, 'input': output, 'bias': dims, 'output': result})
                except ValueError as exc:
                    issues.append(f'例题{contract["example_id"]}广播约束不成立：{exc}')
                for clause in re.split(r'[。；\n]', text):
                    if not re.search(r'广播|误用|偏置', clause):
                        continue
                    for stated in re.finditer(SHAPE, clause):
                        candidate = shape(stated.group())
                        # 反例只对明确的“[形状]就会报错”判定，避免把“形状出错”当运行失败。
                        if re.match(r'\s*(?:就|则|会|便|将|就会|将会)*\s*(?:因[^。；]{0,20})?(?:报错|无法广播|不兼容)', clause[stated.end():]):
                            try:
                                expanded = broadcast(output, candidate)
                            except ValueError:
                                continue
                            issues.append(f'{list(output)}与{list(candidate)}可以广播，结果为{list(expanded)}；不能说运行报错')
                        named = re.search(rf'(?<![A-Za-z0-9_]){re.escape(name)}(?![A-Za-z0-9_])', clause)
                        explicit = re.match(rf'\s*(?:的?\s*(?:shape|形状)\s*(?:是|为|=)?\s*)?({SHAPE})', clause[named.end():]) if named else None
                        if explicit and candidate == shape(explicit[1]):
                            try:
                                expanded = broadcast(output, candidate)
                            except ValueError:
                                continue
                            result_claim = re.search(rf'(?:广播后|结果|得到|变成|变为|扩成|仍为)[^。；]{{0,12}}?({SHAPE})', clause[stated.end():])
                            if result_claim and shape(result_claim[1]) != expanded:
                                issues.append(f'{list(output)}加{name}{list(candidate)}广播结果应为{list(expanded)}')
    for match in re.finditer(r'(?:tf\.)?matmul\([^\n。)]*\)|(?<![A-Za-z0-9_])[A-Za-z]\s*[*@]\s*[A-Za-z](?![A-Za-z0-9_])', text):
        if not any(a <= match.start() and match.end() <= b for a, b in spans):
            uncovered.append(match.group() + '：没有匹配的具名二维约束')
    if re.search(r'广播只(?:能)?(?:作用|发生|用于).{0,4}末轴', text):
        issues.append('广播从末轴逐轴比较，可扩展多个轴，不能概括为只作用于末轴')
    return {'checked': len(checks), 'checks': checks, 'issues': list(dict.fromkeys(issues)),
            'uncovered': list(dict.fromkeys(uncovered)), 'scope': '显式具名二维乘法、输出与有限广播表述；其余数学语义待人工核对'}
