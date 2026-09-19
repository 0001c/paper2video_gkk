#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""gkk 论文三组合生成 —— Python 执行器。

论文：孟欣《镜像与幻象：黑色电影理论的视觉谱系与本土化跨国重构》
人像：assets/gkk/portrait.png

为什么不用 bash 脚本
--------------------
最初写的是 .sh，但它**必崩**：`cd "<含中文的路径>"` 报 No such file or directory。
原因是本机 MSYS/Git Bash 在**独立脚本进程**里的 locale 与交互 shell 不同，
UTF-8 的中文路径字节被按非 UTF-8 编码解释后再交给 Windows，
路径查找必然失败（而交互式命令里同样的路径能正常 cd，极具迷惑性）。
Python 直接把 Unicode 字符串交给 Windows API，没有这层字节转换，
所以在本项目的「路径含中文」前提下，Python 执行器比 shell 稳得多。

用法:
    python scripts/gkk_3combos.py <combo> [stage]
      combo : gkk_en | gkk_en_zh | gkk_zh | all
      stage : 透传给 --stage 的 JSON，默认 ["1","2","3"]
      其余 : 原样追加给 pipeline.py（如 --reuse_script True）

三个组合（语言是两个独立的轴，不是单一开关）:
    gkk_en     幻灯片 英文 + 讲稿 英文
    gkk_en_zh  幻灯片 英文 + 讲稿 中文   ← 国内组会 / 中文答辩最常见的形态
    gkk_zh     幻灯片 中文 + 讲稿 中文

⚠️ 三次运行必须**串行**：16GB 单卡上 MuseTalk 数字人与 GUI-Owl 光标定位都要占显存，
并发会 OOM。本脚本天然串行。
"""
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = os.path.join(ROOT, '.venv', 'Scripts', 'python.exe')
SRC = os.path.join(ROOT, 'Paper2Video-main', 'src')
LOGDIR = os.path.join(ROOT, '.workbuddy', 'logs')

# 输入素材在**仓库根**的 assets/ 下（上游 Paper2Video-main/assets 是只读 demo，
# 不要往里放东西）。这两个相对路径是相对 pipeline 的 cwd（Paper2Video-main/src）。
PAPER = '../../assets/papers/gkk_mengxin'
PORTRAIT = '../../assets/gkk/portrait.png'

COMBOS = {
    'gkk_en':    ('en', 'en'),
    'gkk_en_zh': ('en', 'zh'),
    'gkk_zh':    ('zh', 'zh'),
}


def run_combo(name, slide_lang, script_lang, stage, extra=None):
    log_path = os.path.join(LOGDIR, '{}.log'.format(name))
    os.makedirs(LOGDIR, exist_ok=True)

    cmd = [
        PY, '-u', 'pipeline.py',
        '--stage', stage,
        '--result_dir', '../../result/{}'.format(name),
        '--slide_lang', slide_lang,
        '--script_lang', script_lang,
        '--paper_latex_root', PAPER,
        '--ref_img', PORTRAIT,
        '--gpu_list', '0',
        '--if_tree_search', 'False',
    ]
    if extra:
        cmd += list(extra)

    print('=== {}  slide_lang={} script_lang={} stage={} ==='.format(
        name, slide_lang, script_lang, stage), flush=True)
    print('    日志 -> {}'.format(log_path), flush=True)

    with open(log_path, 'w', encoding='utf-8') as log:
        log.write('### {}   slide_lang={}  script_lang={}  stage={}\n'.format(
            name, slide_lang, script_lang, stage))
        log.write('### 开始 {}\n'.format(time.strftime('%Y-%m-%d %H:%M:%S')))
        log.write('### 命令 {}\n'.format(' '.join(cmd)))
        log.flush()

        t0 = time.time()
        # 与 web 侧 `_run_job` 用同一套环境约定：UTF-8 模式 + 标准流 UTF-8。
        # 少了它们，Windows 上会撞上游那些"不带 encoding 的 open()"
        # （实测：--if_tree_search True 时读 select_proposal.txt 的 0x92 报错）。
        sub_env = dict(os.environ)
        sub_env["PYTHONIOENCODING"] = "utf-8"
        sub_env["PYTHONUTF8"] = "1"
        proc = subprocess.run(cmd, cwd=SRC, stdout=log, stderr=subprocess.STDOUT, env=sub_env)
        dt = time.time() - t0

        log.write('### 退出码={}\n'.format(proc.returncode))
        log.write('### 结束 {}  （耗时 {:.1f} min）\n'.format(
            time.strftime('%Y-%m-%d %H:%M:%S'), dt / 60))

    print('    退出码={}  耗时 {:.1f} min\n'.format(proc.returncode, dt / 60),
          flush=True)
    return proc.returncode, dt


def main():
    combo = sys.argv[1] if len(sys.argv) > 1 else 'all'
    stage = sys.argv[2] if len(sys.argv) > 2 else '["1","2","3"]'
    # 其余参数原样追加给 pipeline.py（例如 --reuse_script True、--paper_type humanities）
    extra = sys.argv[3:]

    if combo == 'all':
        names = ['gkk_en', 'gkk_en_zh', 'gkk_zh']
    elif combo in COMBOS:
        names = [combo]
    else:
        print('未知组合：{}（可选 gkk_en / gkk_en_zh / gkk_zh / all）'.format(combo))
        return 2

    print('ROOT  = {}'.format(ROOT))
    print('论文   = {}\n人像   = {}\n'.format(PAPER, PORTRAIT), flush=True)

    results = []
    for name in names:
        slide_lang, script_lang = COMBOS[name]
        rc, dt = run_combo(name, slide_lang, script_lang, stage, extra)
        results.append((name, rc, dt))

    print('=' * 62)
    print('汇总')
    for name, rc, dt in results:
        print('  {:<10} 退出码={:<3} 耗时 {:>5.1f} min  {}'.format(
            name, rc, dt / 60, 'OK' if rc == 0 else '⚠️ 失败'))
    return 0 if all(rc == 0 for _, rc, _ in results) else 1


if __name__ == '__main__':
    sys.exit(main())
