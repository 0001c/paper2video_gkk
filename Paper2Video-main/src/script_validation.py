"""讲稿结构校验，供流水线和 Web 续跑预检共用，不加载模型依赖。"""


def validate_script_output(text, expected_slides):
    issues = []
    stripped = str(text).strip()
    if stripped.count('"') % 2 != 0:
        issues.append("引号不成对（疑似截断）")
    pages = [page for page in stripped.split("###") if page.strip()]
    if len(pages) < expected_slides:
        issues.append("页数 {} 少于幻灯片张数 {}".format(len(pages), expected_slides))
    blank = [idx + 1 for idx, page in enumerate(pages)
             if not any("|" in line for line in page.splitlines())]
    if blank:
        issues.append("第 {} 页没有任何「讲稿 | 目标」行".format(blank))
    return (not issues), "；".join(issues)
