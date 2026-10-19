"""将受众和时长作为内部教学控制，检查明确的教学包装文案泄漏。"""
import re


CLASSROOM_PROMPT = (
    '\n【教学配置与成品文案的边界】受众身份、年级、预设知识、目标时长和逐页时间预算仅供内部调整难度、深度和节奏，'
    '不是封面、副标题、页脚、正文或口播内容。不要写“研一学生专用”“20分钟核心讲解”，'
    '不要向听众宣告其年级身份或说“在有限的二十分钟里”。'
    '使用教师通用的自然课堂语言，如“同学们”“大家”“这节课”“接下来”；无需逐页重复问候。'
    '学习目标应表达要掌握的知识与能力，不复述配置字段。'
    '即使旧课件图片含这些包装标签，口播也不要照读、解释或扩写它们；光标无需指向这些标签，写no即可。'
    '教材中的研究对象、实验时长、数值、年份和知识事实必须保留，不能把所有学生身份或时间数字一律删除。'
    ' Audience profiles and time budgets are internal guidance, never slide copy or narration. '
    'Use natural classroom wording such as "everyone", "this lesson", and "next"; preserve subject-matter facts.'
)

_AUDIENCE = r'(?:研[一二三123]|大[一二三四1234]|研究生|硕士生|博士生|本科生|高中生|初中生|小学生)(?:学生|同学)?'
_MINUTES = r'(?:\d+(?:\.\d+)?|[零〇一二两三四五六七八九十百半]+)分钟'
_LEAKS = (
    ('受众身份标签', rf'{_AUDIENCE}(?:专用|专属|适用|专版)'),
    ('受众身份宣告', rf'(?:本课|本节课|这节课|本课程|本次讲解)[^。！？;；\n]{{0,16}}(?:面向|针对|适合){_AUDIENCE}'),
    ('受众身份宣告', rf'(?:旨在|希望)(?:帮助|让){_AUDIENCE}(?:快速|更好|掌握|理解|学习)'),
    ('时长包装', rf'{_MINUTES}(?:核心讲解|精讲|速成|课程|讲解|课堂|教学|版)'),
    ('时长宣告', rf'在(?:有限的|接下来的|短短)?{_MINUTES}(?:里|内|中)[，,]?(?:我们|我将)'),
    ('时长宣告', rf'(?:本课|本节课|这节课|本课程|本次讲解)(?:时长为?|将用|用时|预计|约|为|只有|共|总共){_MINUTES}'),
)


def classroom_issues(text, *, latex=False):
    """只拦截明确的配置包装，不按年级词或时间数字全局删改正文。"""
    if latex:
        text = re.sub(r'(?m)(?<!\\)%[^\n]*', '', text)
        text = re.sub(r'\\[A-Za-z]+\*?(?:\[[^\]]*\])?', '', text)
    compact = re.sub(r'[\s{}]', '', text)
    issues = [f'教学配置泄漏（{kind}）：“{match[0]}”；删除包装标签，改用“同学们”“这节课”等自然课堂表达，保留知识事实'
              for kind, pattern in _LEAKS if (match := re.search(pattern, compact))]
    english = re.search(
        r'(?:graduate|undergraduate|doctoral)\s+students\s+only|'
        r'\b\d+[ -]minute\s+(?:core\s+lecture|crash\s+course)', text, re.I)
    if english:
        issues.append(f'Teaching metadata leaked: "{english[0]}"; use natural classroom language without audience or duration labels')
    return issues
