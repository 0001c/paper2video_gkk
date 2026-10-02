# -*- coding: utf-8 -*-
"""核验候选模型在 HuggingFace 上的真实存在性、体量与更新时间。

只用标准库，逐仓库请求 HF 公开 API；主站不可达时回退到 hf-mirror。
"""
import json
import urllib.error
import urllib.request

from download_models import CATALOG

REPOS = list(CATALOG)

ENDPOINTS = ["https://huggingface.co", "https://hf-mirror.com"]
FIELDS = ("downloads", "likes", "lastModified", "pipeline_tag", "library_name")


HEADERS = {
    # HF 前置的 Cloudflare 会拦截 urllib 的默认 UA（实测 403），伪装成浏览器即可放行
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
}


def probe(repo):
    """依次尝试各镜像端点，返回 (端点, 元数据) 或 (None, 错误)。"""
    last_err = "unknown"
    for base in ENDPOINTS:
        url = "{}/api/models/{}".format(base, repo)
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=25) as resp:
                return base.split("//")[1].split(".")[0], json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            last_err = "HTTP {}".format(exc.code)
            if exc.code == 404:
                return None, "404 不存在"
        except Exception as exc:  # 网络层失败，换端点重试
            last_err = type(exc).__name__
    return None, last_err


def list_author(author, limit=60):
    """枚举某作者/组织名下的全部模型，用于确认仓库的真实命名。"""
    for base in ENDPOINTS:
        url = "{}/api/models?author={}&limit={}".format(base, author, limit)
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=25) as resp:
                items = json.loads(resp.read().decode("utf-8"))
            print("[{}] {} 共 {} 个模型".format(base.split("//")[1].split(".")[0], author, len(items)))
            for it in items:
                print("   {:<55} dl={:<10} {}".format(
                    it.get("id", "?"),
                    str(it.get("downloads", "-")),
                    str(it.get("lastModified", "-"))[:19],
                ))
            return
        except Exception as exc:
            print("{} 失败: {}".format(base, exc))
    return


def main():
    import sys

    if len(sys.argv) > 2 and sys.argv[1] == "--author":
        for author in sys.argv[2:]:
            list_author(author)
        return

    print("{:<48} {:<22} {:<9} {:<7} {}".format("repo", "lastModified", "downloads", "likes", "tag"))
    print("-" * 110)
    for repo in REPOS:
        src, info = probe(repo)
        if not isinstance(info, dict):
            print("{:<48} {:<22} {:<9} {:<7} {}".format(repo, "-", "-", "-", info))
            continue
        print("{:<48} {:<22} {:<9} {:<7} {}".format(
            repo,
            str(info.get("lastModified", "-"))[:19],
            str(info.get("downloads", "-")),
            str(info.get("likes", "-")),
            str(info.get("pipeline_tag") or info.get("library_name") or "-"),
        ))


if __name__ == "__main__":
    main()
