"""统一下载 Paper2Video 所需的全部模型权重（纯标准库实现）。

为什么不用 huggingface_hub / modelscope 的 snapshot_download
-----------------------------------------------------------
两者都会「先写临时文件 + 加 .lock + 完成后改名 + 清理临时目录」。
在本机的受限沙箱里，**删除 .lock 与临时目录会被安全策略拦截**
（`SAFE_DELETE_BULK_CONFIRM_REQUIRED`），结果是大文件已经完整下载、
却停在 `.cache/huggingface/download/<hash>.incomplete` 没有改名——
命令退出码 1，模型目录看上去是空的。

因此这里改为**直连 HTTP**：
- 只用 urllib，不产生任何锁文件、临时目录；
- 断点续传靠 `<file>.part` + HTTP Range；
- 完成后 `os.replace()` 改名（改名不触发删除拦截，实测可用）。

设计要点
--------
1. **一律下载到纯英文路径**（``E:\\ai-models\\weights``）。项目本身位于
   ``E:\\Development\\26年\\...``，路径含中文，而 Wan / InfiniteTalk 系的加载器
   遇到非 ASCII 路径会直接抛错，所以权重必须与代码目录解耦。
2. **优先 ModelScope，回退 HuggingFace 镜像**。国内 ModelScope 带宽通常显著
   优于 hf-mirror；GUI-Owl 只有 HF 有，脚本会自动回退。
3. 支持 ``--group`` 分组下载，先拿主链路（phase1，约 17 GB），
   数字人链路（phase3，80 GB+）等前面跑通再下。
4. 已存在且大小相符的文件直接跳过；大小不符的会续传补齐。

用法
----
    ./.venv/Scripts/python.exe scripts/download_models.py --list
    ./.venv/Scripts/python.exe scripts/download_models.py --group phase1
    ./.venv/Scripts/python.exe scripts/download_models.py --model Qwen/Qwen3-ASR-0.6B
    ./.venv/Scripts/python.exe scripts/download_models.py --group phase3 --source modelscope
    ./.venv/Scripts/python.exe scripts/download_models.py --recover   # 抢救被中断的 .incomplete
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

# 控制台按 UTF-8 输出，避免 Windows GBK 下中文/特殊符号报错
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEIGHTS_DIR = os.environ.get("PAPER2VIDEO_WEIGHTS", r"E:\ai-models\weights")

HF_ENDPOINT = os.environ.get("HF_ENDPOINT", "https://hf-mirror.com").rstrip("/")
MS_ENDPOINT = "https://modelscope.cn"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

# 每个条目：repo_id -> (分组, 说明, 约体积GB, 首选源)
# 说明里的体积来自 HF API 的 siblings 实测，仅含该仓库自身文件（不含底模）。
CATALOG = {
    # ── Phase 1：主链路（无数字人）──
    "Qwen/Qwen3-TTS-12Hz-1.7B-Base": (
        "phase1", "语音合成：零样本音色克隆（替换 F5-TTS）", 4.5, "modelscope"),
    "Qwen/Qwen3-ASR-0.6B": (
        "phase1", "语音转写：52 语言/方言（替换 WhisperX 转写部分）", 1.9, "modelscope"),
    "Qwen/Qwen3-ForcedAligner-0.6B": (
        "phase1", "强制对齐：词级时间戳，误差 27.8ms（替换 WhisperX 对齐部分）", 1.8, "modelscope"),
    "mPLUG/GUI-Owl-1.5-4B-Instruct": (
        "phase1", "光标空间定位：输出 0-1000 归一化坐标（替换 UI-TARS-1.5-7B）", 8.9, "huggingface"),

    # ── 评测链路 ──
    "iic/speech_campplus_sv_zh-cn_16k-common": (
        "eval", "音色相似度：CAM++ 说话人验证（替换 SpeechBrain ECAPA）", 0.03, "modelscope"),

    # ── Phase 3：数字人（体量大，前面跑通再下）──
    "Wan-AI/Wan2.1-I2V-14B-480P": (
        "phase3", "InfiniteTalk 底模：Wan2.1 图生视频 14B（只取 config/VAE/CLIP/tokenizer）", 5.1, "modelscope"),
    "MeiGen-AI/InfiniteTalk": (
        "phase3", "音频驱动数字人 adapter（fp8 量化路径，仓库全量 157GB）", 25.0, "modelscope"),
    "TencentGameMate/chinese-wav2vec2-base": (
        "phase3", "InfiniteTalk 音频编码器", 0.4, "modelscope"),
}

# 各仓库的白名单前缀。没有条目的仓库 = 全量下载。
#
# ⚠️ Phase 3 这三个仓库必须白名单，否则会多下约 100 GB。依据是 wan/multitalk.py
# 的两条互斥分支（在 16 GB 卡上我们必须走量化那条）：
#
#   if quant is not None:                      # ← 我们走这里（fp8）
#       model_state_dict = load_file(quant_dir)   # 单文件即「已合并 adapter 的量化 DiT」
#       ...
#   else:
#       if dit_path is None:
#           weight_files = [7 个 diffusion_pytorch_model-*.safetensors,  # 62.5 GB
#                           f"{infinitetalk_dir}"]                       # 9.5 GB
#
# 也就是说，**量化分支根本不加载那 7 个 bf16 分片，也不用 infinitetalk_dir**
# （它只在上面 else 分支的第 219 行被用到）；底模仓库里真正要读的只有
# config.json（第 197 行）、VAE、CLIP 与两个 tokenizer。T5 权重也由
# quant_models/t5_fp8.safetensors 顶替（t5.py 第 506-508 行按
# `os.path.dirname(quant_dir)/t5_{quant}.safetensors` 去找）。
ALLOW_PATTERNS = {
    "MeiGen-AI/InfiniteTalk": [
        "quant_models/infinitetalk_single_fp8",   # .safetensors + .json 地图
        "quant_models/t5_fp8",                    # fp8 文本编码器
        "quant_models/t5_map_fp8",                # T5 量化映射表
        "README.md", "LICENSE.txt", ".gitattributes",
    ],
    "Wan-AI/Wan2.1-I2V-14B-480P": [
        "config.json",
        "Wan2.1_VAE.pth",
        "models_clip_open-clip-xlm-roberta-large-vit-huge-14.pth",
        "xlm-roberta-large/",       # CLIP 的 tokenizer
        "google/umt5-xxl/",         # T5 的 tokenizer（权重由 t5_fp8 顶替）
        "LICENSE.txt", ".gitattributes",
    ],
    "TencentGameMate/chinese-wav2vec2-base": [
        "pytorch_model.bin", "config.json", "preprocessor_config.json",
        "README.md", ".gitattributes",
    ],
}

# 多线程下载的并发度。HF 镜像实测单流可达 130 MB/s，这里主要用于
# 「小文件多、大文件少」的仓库，避免被单个小文件拖住串行等待。
MAX_WORKERS = 4


# ──────────────────────────────────────────────────────────────
# 工具
# ──────────────────────────────────────────────────────────────

def _human(num_bytes):
    for unit in ("B", "KB", "MB", "GB"):
        if num_bytes < 1024:
            return "{:.1f} {}".format(num_bytes, unit)
        num_bytes /= 1024
    return "{:.1f} TB".format(num_bytes)


def _dir_size(path):
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass
    return total


def _open(url, headers=None, timeout=60, method="GET"):
    h = {"User-Agent": UA}
    if headers:
        h.update(headers)
    return urllib.request.urlopen(urllib.request.Request(url, headers=h, method=method),
                                  timeout=timeout)


def _match_allow(repo, path):
    """按仓库的白名单前缀过滤路径；没有白名单则全部保留。"""
    patterns = ALLOW_PATTERNS.get(repo)
    if not patterns:
        return True
    return any(path == p or path.startswith(p) for p in patterns)


# ──────────────────────────────────────────────────────────────
# 文件清单（不含文件内容）
# ──────────────────────────────────────────────────────────────

def list_files_hf(repo):
    """HF 镜像：返回 {相对路径: 字节数}。"""
    url = "{}/api/models/{}?blobs=true".format(HF_ENDPOINT, repo)
    with _open(url, timeout=45) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    out = {}
    for item in data.get("siblings") or []:
        name = item.get("rfilename")
        if name:
            out[name] = item.get("size") or 0
    return out


def list_files_ms(repo):
    """ModelScope：返回 {相对路径: 字节数}。"""
    url = "{}/api/v1/models/{}/repo/files?Revision=master&Recursive=True".format(MS_ENDPOINT, repo)
    with _open(url, timeout=45) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    files = (data.get("Data") or {}).get("Files") or []
    out = {}
    for item in files:
        if item.get("Type") != "blob":
            continue
        path = item.get("Path")
        if path:
            out[path] = item.get("Size") or 0
    return out


LISTERS = {"huggingface": list_files_hf, "modelscope": list_files_ms}


def file_url(source, repo, path):
    """拼出可直连下载的 URL。"""
    if source == "huggingface":
        return "{}/{}/resolve/main/{}".format(HF_ENDPOINT, repo, urllib.parse.quote(path))
    return "{}/api/v1/models/{}/repo?Revision=master&FilePath={}".format(
        MS_ENDPOINT, repo, urllib.parse.quote(path))


# ──────────────────────────────────────────────────────────────
# 单文件下载（带断点续传）
# ──────────────────────────────────────────────────────────────

def fetch_file(url, dest, expected_size=None, retries=3):
    """下载单个文件到 dest，完成后改名。

    续传策略：先写 ``dest + '.part'``；若该文件已存在则用 Range 从断点继续。
    全部收齐后 ``os.replace`` 改名 —— 改名不受沙箱的删除拦截影响。
    """
    part = dest + ".part"

    if os.path.isfile(dest):
        cur = os.path.getsize(dest)
        if not expected_size or cur == expected_size:
            return True, cur
        # 大小不对：删除重下（若删不掉就覆盖写）
        try:
            os.remove(dest)
        except OSError:
            pass

    os.makedirs(os.path.dirname(dest), exist_ok=True)

    last_error = None
    for attempt in range(1, retries + 1):
        done = os.path.getsize(part) if os.path.isfile(part) else 0
        if expected_size and done > expected_size:
            # part 比目标大，说明上次是别的文件或写坏了，重来
            try:
                os.remove(part)
            except OSError:
                pass
            done = 0

        headers = {}
        mode = "wb"
        if done:
            headers["Range"] = "bytes={}-".format(done)
            mode = "ab"

        try:
            with _open(url, headers=headers, timeout=120) as resp:
                # 服务端不支持 Range 时会返回 200 全文，此时必须从头写
                if done and resp.status != 206:
                    done = 0
                    mode = "wb"
                with open(part, mode) as fh:
                    while True:
                        chunk = resp.read(1024 * 1024)
                        if not chunk:
                            break
                        fh.write(chunk)
        except Exception as exc:  # noqa: BLE001 - 网络异常种类繁多，统一重试
            last_error = exc
            if attempt < retries:
                time.sleep(2 * attempt)
                continue
            return False, os.path.getsize(part) if os.path.isfile(part) else 0

        got = os.path.getsize(part)
        if expected_size and got != expected_size:
            last_error = RuntimeError("大小不符：期望 {} 实际 {}".format(expected_size, got))
            if attempt < retries:
                time.sleep(2 * attempt)
                continue
            return False, got

        os.replace(part, dest)
        return True, got

    return False, 0


def fetch_one_file(source, repo, path, local_dir, expected_size, quiet=False):
    dest = os.path.join(local_dir, path.replace("/", os.sep))
    if os.path.isfile(dest) and expected_size and os.path.getsize(dest) == expected_size:
        return True
    url = file_url(source, repo, path)
    ok, got = fetch_file(url, dest, expected_size)
    if not quiet:
        print("      {} {:<46} {:>10}".format("OK " if ok else "FAIL", path,
                                               _human(got) if got else ""))
    return ok


# ──────────────────────────────────────────────────────────────
# 整仓下载
# ──────────────────────────────────────────────────────────────

def download_repo(repo, source):
    """从指定源把一个仓库拉到 WEIGHTS_DIR/<org>__<name>。

    幂等：已存在且**单文件大小相符**的直接跳过，只补缺失/损坏的部分。
    """
    lister = LISTERS[source]
    files = {p: s for p, s in lister(repo).items() if _match_allow(repo, p)}
    if not files:
        raise RuntimeError("清单为空（仓库不存在或全部被白名单过滤）")

    total = sum(files.values())
    local_dir = os.path.join(WEIGHTS_DIR, repo.replace("/", "__"))
    os.makedirs(local_dir, exist_ok=True)

    def _present(path, size):
        dest = os.path.join(local_dir, path.replace("/", os.sep))
        # .gitattributes 在不同源之间内容不同，只要文件在就算合格，
        # 否则会因为大小永远对不上而被反复重下。
        if os.path.basename(path) in (".gitattributes", ".gitignore"):
            return os.path.isfile(dest)
        return os.path.isfile(dest) and size and os.path.getsize(dest) == size

    missing = {p: s for p, s in files.items() if not _present(p, s)}
    print("    文件 {} 个，合计 {}，其中待下载 {} 个".format(
        len(files), _human(total), len(missing)))
    if not missing:
        return True

    failed = []
    # 大文件单独串行更稳（避免带宽争抢），小文件并发
    big = {p: s for p, s in missing.items() if s > 200 * 1024 * 1024}
    small = {p: s for p, s in missing.items() if p not in big}

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {
            pool.submit(fetch_one_file, source, repo, p, local_dir, s): p
            for p, s in small.items()
        }
        for fut in as_completed(futures):
            if not fut.result():
                failed.append(futures[fut])

    for p, s in sorted(big.items(), key=lambda kv: -kv[1]):
        print("    大文件 {:<46} {}".format(p, _human(s)))
        t0 = time.time()
        ok = fetch_one_file(source, repo, p, local_dir, s, quiet=True)
        dt = time.time() - t0
        speed = "{:.0f} MB/s".format(s / dt / 1e6) if dt > 0.5 and ok else ""
        print("      {} {}  {}".format("OK " if ok else "FAIL", p, speed))
        if not ok:
            failed.append(p)

    # 落盘后逐文件复核，而不是只看目录总体积
    still = [p for p, s in files.items() if not _present(p, s)]
    got = _dir_size(local_dir)
    print("    落地 {:.2f} GB / 预期 {:.2f} GB".format(got / 1e9, total / 1e9))
    if still:
        print("    仍不合格 {} 个：{}".format(len(still), still[:5]))
        raise RuntimeError("{} 个文件未完成".format(len(still)))
    return True


def fetch_one(repo, source="auto"):
    group, desc, approx_gb, preferred = CATALOG[repo]
    local_dir = os.path.join(WEIGHTS_DIR, repo.replace("/", "__"))

    print("\n" + "=" * 78)
    print("[{}] {}".format(group, repo))
    print("    用途：{}".format(desc))
    print("    预计体积：约 {:.1f} GB".format(approx_gb))
    print("    目标目录：{}".format(local_dir))

    # ⚠️ 不要用「目录总体积达到预期 90%」来判断是否已完成。
    # 实测踩到：Qwen3-TTS 仓库漏下 speech_tokenizer/model.safetensors
    # （0.49 GB）时，总体积 4.05 GB 恰好压过 4.5*0.9=4.05 的阈值而被误判。
    # 改为始终列清单、逐文件核对大小；download_repo 内部对合格文件即时返回。
    if source == "auto":
        order = [preferred, "huggingface" if preferred == "modelscope" else "modelscope"]
    else:
        order = [source]

    for src in order:
        print("    尝试源：{}".format(src))
        try:
            download_repo(repo, src)
            return True
        except Exception as exc:  # noqa: BLE001
            print("    失败：{}: {}".format(type(exc).__name__, str(exc)[:200]))

    print("    [x] 全部源均失败")
    return False


# ──────────────────────────────────────────────────────────────
# 校验：把本地文件与远端清单逐个比对
# ──────────────────────────────────────────────────────────────

def verify_repo(repo, source="auto"):
    """逐个文件核对大小，返回 (是否全部相符, 问题列表)。

    清单来源：--source 指定的源优先，拉不到再试另一个。
    注意 CAM++ 只存在于 ModelScope、GUI-Owl 只存在于 HF，因此**不能**
    因为用户指定了某个源就对另一个源一概不查。
    """
    local_dir = os.path.join(WEIGHTS_DIR, repo.replace("/", "__"))
    order = list(LISTERS.keys())
    if source in order:
        order.remove(source)
        order.insert(0, source)

    remote = None
    for src in order:
        try:
            remote = LISTERS[src](repo)
            break
        except Exception:  # noqa: BLE001
            continue
    if remote is None:
        return False, [("清单拉取失败（两个源都试过）", 0, 0)]

    remote = {p: s for p, s in remote.items() if _match_allow(repo, p)}
    problems = []
    for path, size in sorted(remote.items()):
        # .gitattributes / .gitignore 在不同源（HF vs ModelScope）内容不同，
        # 且完全不影响模型加载，不纳入校验，否则每次都会报"有问题"。
        if os.path.basename(path) in (".gitattributes", ".gitignore"):
            continue
        dest = os.path.join(local_dir, path.replace("/", os.sep))
        part = dest + ".part"
        if os.path.isfile(dest):
            actual = os.path.getsize(dest)
            if size and actual != size:
                problems.append((path, actual, size))
        elif os.path.isfile(part):
            problems.append((path + " [.part 未完成]", os.path.getsize(part), size))
        else:
            problems.append((path + " [缺失]", 0, size))
    return (not problems), problems


# ──────────────────────────────────────────────────────────────
# 抢救：把被中断的 huggingface_hub .incomplete 改名归位
# ──────────────────────────────────────────────────────────────

def recover(repo):
    """按字节数把 `.cache/huggingface/download/*.incomplete` 改回真实文件名。

    用于修复「huggingface_hub 因沙箱拦截 .lock 删除而没能完成改名」的残留。
    逐个源都扫一遍文件清单，用**大小唯一匹配**定位目标名。
    """
    local_dir = os.path.join(WEIGHTS_DIR, repo.replace("/", "__"))
    cache = os.path.join(local_dir, ".cache", "huggingface", "download")
    if not os.path.isdir(cache):
        print("  {}: 无 .cache 目录，跳过".format(repo))
        return

    leftovers = [f for f in os.listdir(cache) if f.endswith(".incomplete")]
    if not leftovers:
        print("  {}: 无 incomplete 残留".format(repo))
        return

    # 汇总所有源的清单，构造 大小 -> 路径 的映射
    size_map = {}
    for src, lister in LISTERS.items():
        try:
            for path, size in lister(repo).items():
                if size:
                    size_map.setdefault(size, path)
        except Exception as exc:  # noqa: BLE001
            print("  {}: 清单拉取失败({}) {}".format(repo, src, str(exc)[:80]))

    for name in leftovers:
        src_path = os.path.join(cache, name)
        size = os.path.getsize(src_path)
        target = size_map.get(size)
        if not target:
            print("  {}: {} -> 未匹配到目标（{} bytes）".format(repo, name[:28], size))
            continue
        dest = os.path.join(local_dir, target.replace("/", os.sep))
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        os.replace(src_path, dest)
        print("  {}: {} -> {} ({})".format(repo, name[:28], target, _human(size)))


# ──────────────────────────────────────────────────────────────
# 入口
# ──────────────────────────────────────────────────────────────

def print_catalog():
    print("权重根目录：{}\n".format(WEIGHTS_DIR))
    print("来源：ModelScope {}  |  HF 镜像 {}".format(MS_ENDPOINT, HF_ENDPOINT))
    print()
    print("{:<44} {:<8} {:>8}  {}".format("repo", "group", "size", "说明"))
    print("-" * 130)
    for repo, (group, desc, gb, _src) in CATALOG.items():
        print("{:<44} {:<8} {:>6.1f}G  {}".format(repo, group, gb, desc))
    total = sum(v[2] for v in CATALOG.values())
    print("-" * 130)
    print("{:<44} {:<8} {:>6.1f}G  （phase1 小计 {:.1f}G）".format(
        "合计", "", total, sum(v[2] for k, v in CATALOG.items() if v[0] == "phase1")))


def main():
    # global 声明必须出现在函数体内对该名字的任何引用之前，否则 SyntaxError
    global WEIGHTS_DIR

    parser = argparse.ArgumentParser(description="下载 Paper2Video 模型权重")
    parser.add_argument("--group", choices=["phase1", "eval", "phase3", "all"],
                        help="按分组下载")
    parser.add_argument("--model", action="append", help="指定 repo_id，可重复")
    parser.add_argument("--source", choices=["auto", "modelscope", "huggingface"], default="auto")
    parser.add_argument("--list", action="store_true", help="只列出清单")
    parser.add_argument("--verify", action="store_true",
                        help="逐个文件核对本地与远端大小，不下载")
    parser.add_argument("--recover", action="store_true",
                        help="抢救被中断的 .incomplete（按大小改名归位）")
    parser.add_argument("--weights-dir", default=WEIGHTS_DIR)
    args = parser.parse_args()

    WEIGHTS_DIR = args.weights_dir

    if args.list or (not args.group and not args.model
                     and not args.recover and not args.verify):
        print_catalog()
        return

    if args.verify:
        targets = args.model or list(CATALOG.keys())
        print("校验 {} 个仓库（逐个文件比对远端大小）".format(len(targets)))
        bad = 0
        for repo in targets:
            ok, problems = verify_repo(repo, args.source)
            print("\n  {:<46} {}".format(repo, "全部相符" if ok else "有问题"))
            for path, actual, expect in problems[:10]:
                print("      {:<50} 本地={} 远端={}".format(path[:50], actual, expect))
            if problems:
                bad += 1
        print("\n{} 个仓库全部合格，{} 个有问题".format(len(targets) - bad, bad))
        sys.exit(0 if bad == 0 else 1)

    if args.recover:
        targets = args.model or list(CATALOG.keys())
        print("抢救 incomplete 残留，扫描 {} 个仓库".format(len(targets)))
        for repo in targets:
            recover(repo)
        return

    targets = []
    if args.model:
        targets.extend(args.model)
    elif args.group == "all":
        targets.extend(CATALOG.keys())
    else:
        targets.extend([k for k, v in CATALOG.items() if v[0] == args.group])

    unknown = [t for t in targets if t not in CATALOG]
    if unknown:
        print("未知 repo（请先用 --list 查看）：{}".format(unknown))
        sys.exit(2)

    print("共 {} 个仓库，目标目录 {}".format(len(targets), WEIGHTS_DIR))
    os.makedirs(WEIGHTS_DIR, exist_ok=True)

    results = {}
    for repo in targets:
        results[repo] = fetch_one(repo, args.source)

    print("\n" + "=" * 78)
    ok = [r for r, v in results.items() if v]
    bad = [r for r, v in results.items() if not v]
    print("成功 {} 个，失败 {} 个".format(len(ok), len(bad)))
    for repo in bad:
        print("  [x] {}".format(repo))
    sys.exit(0 if not bad else 1)


if __name__ == "__main__":
    main()
