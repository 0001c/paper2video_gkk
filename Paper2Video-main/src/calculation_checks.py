"""有限语法的确定性计算核验；不执行文本代码，不把未解析公式算作已验证。"""
import math
import re
import unicodedata

NUMBER = r'[-+负正]?(?:\d+(?:\.\d+)?|[零〇一二两三四五六七八九十百千万]+(?:点[零〇一二两三四五六七八九]+)?)'
# 与教材约束中的六位有效数字输出一致，允许舍入而非放宽到百分比误差。
REL_TOL = 5e-6


def math_text(text):
    text = text.replace('²','**2').replace('³','**3')
    text = unicodedata.normalize('NFKC',text)
    return text.translate(str.maketrans({'−':'-','×':'*','·':'*','÷':'/','^':'**'}))


def numeric(text):
    text = text.strip().replace('负','-').replace('正','+')
    try:
        return float(text)
    except ValueError:
        digits = {c:i for i,c in enumerate('零一二三四五六七八九')}
        digits.update({'〇':0,'两':2})
        sign = -1 if text.startswith('-') else 1
        integer, _, fraction = text.lstrip('+-').partition('点')
        if not integer or any(c not in digits and c not in '十百千万' for c in integer):
            raise ValueError('不是可解析数值')
        if not any(c in integer for c in '十百千万'):
            value = int(''.join(str(digits[c]) for c in integer))
        else:
            value, current = 0, 0
            for char in integer:
                if char in digits:
                    current = digits[char]
                elif char == '万':
                    value, current = (value+current)*10000,0
                else:
                    value, current = value+(current or 1)*{'十':10,'百':100,'千':1000}[char],0
            value += current
        return sign*(value + (float('0.'+''.join(str(digits[c]) for c in fraction)) if fraction else 0))


def _rhs_value(rhs):
    # 先取同一等式链的最后一项，避免把2+3=5的2当最终结果。
    clause = re.split(r'[,，；;。\n]',rhs)[0].strip()
    last = re.split(r'=|等于',clause)[-1].strip()
    match = re.match('('+NUMBER+r')(?![\d零〇一二两三四五六七八九十百千万点])',last)
    if match and not re.match(r'\s*(?:[+*/\-]|加|减|乘|除|的?次方|平方|立方|路|条|倍|分之|项贡献|个分支|[A-Za-z0-9_])',last[match.end():]):
        return numeric(match[1])
    match = re.search(r'(?:结果|总和|总梯度)(?:是|为|等于)\s*('+NUMBER+')',rhs)
    if match:
        return numeric(match[1])
    match = re.search(r'等于\s*('+NUMBER+r')\s*$',rhs)
    return numeric(match[1]) if match else None


def claims(text, variable, derivative=None):
    text = math_text(text)
    if derivative is None:
        lhs = r'(?<![A-Za-z0-9_])'+re.escape(variable)+r'(?![A-Za-z0-9_])'
    else:
        out, inp = re.escape(variable), re.escape(derivative)
        lhs = (rf'(?:∂\s*{out}\s*/\s*∂\s*{inp}|d\s*{out}\s*/\s*d\s*{inp}'
               rf'|{out}\s*对\s*{inp}\s*的?(?:偏导数?|导数)|{out}对于{inp}的?(?:偏导数?|导数))')
    values = []
    # 零宽匹配保留同一句里重复出现的赋值，不能让首个 RHS 吞掉后一个矛盾结果。
    for match in re.finditer(r'(?='+lhs+r'\s*(?:=|等于|是|为|[:：])\s*([^。；;\n]{1,160}))',text):
        value = _rhs_value(match[1])
        if value is not None:
            values.append(value)
    if derivative is not None:
        # 口述分支例题常用“而b……两路……总和是5”，不再重复念导数符号。
        for match in re.finditer(r'(?:而|输入|变量)\s*'+re.escape(derivative)+r'(?![A-Za-z])[\s\S]{0,220}?(?:总和|总梯度)(?:是|为|等于)\s*('+NUMBER+')',text):
            values.append(numeric(match[1]))
    return values


def example_issues(text, example):
    result, issues = example['result'], []
    expected = [(result['variable'],None,result['value'])] + [(result['variable'],key,value) for key,value in result['gradients'].items()]
    for out, inp, expected_value in expected:
        values = claims(text,out,inp)
        lhs = out if inp is None else f'∂{out}/∂{inp}'
        if not values:
            issues.append(f'例题{example["id"]}未完整兑现已核验结果 {lhs}={expected_value:g}，请明确给出结果')
        elif any(not math.isclose(v,expected_value,rel_tol=REL_TOL,abs_tol=1e-8) for v in values):
            issues.append(f'例题{example["id"]}的 {lhs} 有与本地核验不一致的数值，正确值{expected_value:g}')
    return issues


def arithmetic_report(text):
    from teaching_quality import _calculate
    text = math_text(text)
    issues, warnings, checks = [], [], []
    # 仅核验显式常数等式，日期/编号以及含未知变量的式子不会被拿来猜答案。
    for match in re.finditer(r'(?<![A-Za-z0-9_.])([-+]?\d[\d.\s+*/()\-]{0,65})\s*=\s*([-+]?\d+(?:\.\d+)?)(?![\d.])',text):
        expression, stated = match[1].strip(), float(match[2])
        if not any(op in expression for op in '+-*/'):
            continue
        try:
            actual, _, _ = _calculate(expression,{})
        except (ValueError,SyntaxError,ZeroDivisionError,OverflowError):
            continue
        checks.append({'expression':expression,'stated':stated,'calculated':actual})
        if not math.isclose(actual,stated,rel_tol=REL_TOL,abs_tol=1e-8):
            issues.append(f'算术等式{expression}={stated:g}不成立，应为{actual:g}')
    # 支持单变量显式函数、给定初值/更新值，以及“损失从X降至Y”的可复算补充例子。
    definitions = list(re.finditer(r'(?<![A-Za-z])([Jf])(?:\(([a-z])\))?\s*=\s*([a-z\d().+*/ \-]{3,100})',text))
    for index, match in enumerate(definitions):
        expression = match[3].strip()
        variables = set(re.findall(r'\b[a-z]\b',expression))
        if len(variables) != 1:
            continue
        variable = next(iter(variables))
        window = text[match.end():min(match.end()+900,definitions[index+1].start() if index+1<len(definitions) else len(text))]
        assignment = variable+r'\s*=\s*([-+]?\d+(?:\.\d+)?)(?!\d)'
        old = re.search(r'(?:当前|初始|从)\s*'+assignment,window) or re.search(r'(?<![A-Za-z_])'+assignment,window)
        new_values = claims(window,variable+'_new')
        loss = re.search(r'损失(?:值)?从\s*('+NUMBER+r')\s*(?:降低到|下降到|降至|降到|变为|升至|升到)\s*('+NUMBER+')',window)
        before = None
        if old:
            try:
                before, gradients, _ = _calculate(expression,{variable:float(old[1])})
            except (ValueError,SyntaxError,ZeroDivisionError,OverflowError):
                warnings.append('补充函数例子未能本地复算，请人工核对；未将其标为验证通过')
                continue
            for stated in claims(window,match[1],variable):
                actual = gradients[variable]
                checks.append({'expression':expression,'quantity':f'd{match[1]}/d{variable}','stated':stated,'calculated':actual})
                if not math.isclose(stated,actual,rel_tol=REL_TOL,abs_tol=1e-8):
                    issues.append(f'补充例子的d{match[1]}/d{variable}写成{stated:g}，按初值求导应为{actual:g}')
            step = re.search(r'(?<![A-Za-z_])(?:eta|η|学习率)\s*(?:=|等于|为)\s*('+NUMBER+')',window)
            if step and new_values:
                actual = float(old[1])-numeric(step[1])*gradients[variable]
                for stated in new_values:
                    checks.append({'expression':expression,'quantity':variable+'_new','stated':stated,'calculated':actual})
                    if not math.isclose(stated,actual,rel_tol=REL_TOL,abs_tol=1e-8):
                        issues.append(f'补充例子的更新值写成{stated:g}，按负梯度与给定学习率应为{actual:g}')
        if before is not None and new_values and loss:
            try:
                after, _, _ = _calculate(expression,{variable:new_values[-1]})
            except (ValueError,SyntaxError,ZeroDivisionError,OverflowError):
                warnings.append('补充函数更新值未能本地复算，请人工核对')
                continue
            for label, stated, actual in [('更新前损失',numeric(loss[1]),before),('更新后损失',numeric(loss[2]),after)]:
                checks.append({'expression':expression,'quantity':label,'stated':stated,'calculated':actual})
                if not math.isclose(stated,actual,rel_tol=REL_TOL,abs_tol=1e-8):
                    issues.append(f'{label}写成{stated:g}，按给定函数计算应为{actual:g}')
        elif '例' in text[max(0,match.start()-30):match.start()]:
            warnings.append('补充函数例子缺少可解析的初值、更新值或结果，请核对新增数值与结论')
    symbolic = re.findall(r'(?<![A-Za-z0-9_])(?:[A-Za-z][A-Za-z0-9_]*(?:\([A-Za-z]\))?|∂[^=。；]{1,20})\s*=\s*[^。；;\n，,]{1,100}',text)
    uncovered = [expression for expression in symbolic if re.search(r'[A-Za-z]',expression.split('=',1)[-1])]
    return {'checked':len(checks),'checks':checks,'issues':list(dict.fromkeys(issues)), 'uncovered':list(dict.fromkeys(uncovered)),
            'warnings':list(dict.fromkeys(warnings)), 'scope':'显式常数等式、已核验教材结果、可解析单变量损失更新；不覆盖全部数学语义'}
