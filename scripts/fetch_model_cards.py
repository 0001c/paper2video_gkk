"""抓取候选模型的原始 model card（README.md），用于核对真实推理接口。

HF 官方站在国内常被阻断，因此默认走镜像的 /raw/ 端点拿纯文本。
输出写入 docs/model_cards/<组织>__<模型>.md，便于离线查阅与 diff。

用法：
    ./.venv/Scripts/python.exe scripts/fetch_model_cards.py
    ./.venv/Scripts/python.exe scripts/fetch_model_cards.py --repo Qwen/Qwen3-TTS-12Hz-1.7B-Base
"""

import os
import sys
import json
import argparse
import urllib.request

ENDPOINTS = [
    "https://hf-mirror.com",
    "https://huggingface.co",
]

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# 本次需要核对的模型：本地替换方案的全部入选者 + 备选
DEFAULT_REPOS = [
    "Qwen/Qwen3-TTS-12Hz-1.7B-Base",
    "Qwen/Qwen3-ASR-0.6B",
    "Qwen/Qwen3-ForcedAligner-0.6B",
    "mPLUG/GUI-Owl-1.5-4B-Instruct",
    "MeiGen-AI/InfiniteTalk",
    "OmniAvatar/OmniAvatar-1.3B",
    "Wan-AI/Wan2.2-S2V-14B",
]

OUT_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "docs", "model_cards"
)


def fetch_readme(repo):
    """依次尝试各端点抓取 README.md 原文，返回 (来源, 文本) 或 (None, 错误)。"""
    last_err = "unknown"
    for base in ENDPOINTS:
        url = "{}/{}/raw/main/README.md".format(base, repo)
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return base.split("//")[1].split("/")[0], resp.read().decode("utf-8", "replace")
        except Exception as exc:  # 网络/404/403 都继续试下一个端点
            last_err = "{}: {}".format(type(exc).__name__, exc)
    return None, last_err


def fetch_meta(repo):
    """顺带取一次 API 元数据（许可证 / 参数量 / 下载量）。"""
    for base in ENDPOINTS:
        url = "{}/api/models/{}".format(base, repo)
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception:
            continue
    return None


def main():
    parser = argparse.ArgumentParser(description="抓取 HF model card 原文")
    parser.add_argument("--repo", action="append", help="指定仓库，可重复；缺省用内置清单")
    args = parser.parse_args()

    repos = args.repo or DEFAULT_REPOS
    os.makedirs(OUT_DIR, exist_ok=True)

    print("{:<44} {:<22} {:<12} {:<8} {}".format("repo", "source", "license", "params", "bytes"))
    print("-" * 100)

    for repo in repos:
        meta = fetch_meta(repo)
        license_name, params = "-", "-"
        if isinstance(meta, dict):
            license_name = str((meta.get("cardData") or {}).get("license") or "-")
            total = ((meta.get("safetensors") or {}).get("total"))
            if total:
                params = "{:.2f}B".format(total / 1e9)

        source, text = fetch_readme(repo)
        if source is None:
            print("{:<44} {:<22} {:<12} {:<8} {}".format(repo, "FAILED", license_name, params, text))
            continue

        flat = repo.replace("/", "__")
        out_path = os.path.join(OUT_DIR, flat + ".md")
        with open(out_path, "w", encoding="utf-8") as handle:
            handle.write("<!-- 来源: {} -->\n\n".format(repo))
            handle.write(text)

        print("{:<44} {:<22} {:<12} {:<8} {}".format(
            repo, source, license_name, params, len(text.encode("utf-8"))))

    print("\n已写入:", OUT_DIR)


if __name__ == "__main__":
    main()
