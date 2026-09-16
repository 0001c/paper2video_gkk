"""安装外部命令行工具（当前：tectonic）。

为什么需要 tectonic
------------------
原仓库 ``slide_code_gen_select_improvement.compile_tex()`` 直接调用
``subprocess.run(["tectonic", str(tex_path)])``。tectonic 是一个单文件
LaTeX 引擎，会按需自动下载所需宏包 —— 因此**不需要**安装数 GB 的
TeX Live / MiKTeX。

安装位置固定为 ``E:\\ai-models\\bin``（纯英文路径），并由 ``pv_env`` 在启动时
前置到 PATH，所以不需要改系统环境变量。

用法：
    ./.venv/Scripts/python.exe scripts/setup_tools.py
    ./.venv/Scripts/python.exe scripts/setup_tools.py --mirror
"""

import os
import io
import sys
import zipfile
import argparse
import urllib.request

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

INSTALL_DIR = r"E:\ai-models\bin"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

RELEASE_API = "https://api.github.com/repos/tectonic-typesetting/tectonic/releases/latest"
RELEASE_DL = "https://github.com/tectonic-typesetting/tectonic/releases/download/"
# 实测（2026-09 本机网络）：api.github.com 与 release CDN 均直连失败（502 隧道），
# 但下列加速前缀可用。因此 API 与二进制都按前缀依次尝试。
DOWNLOAD_MIRRORS = ["https://ghproxy.net/", "https://ghfast.top/", ""]

# API 不可达时的兜底：直接按已知版本构造下载地址
KNOWN_ASSETS = [
    ("tectonic@0.17.0", "tectonic-0.17.0-x86_64-pc-windows-gnu.zip"),
]


def _get(url, timeout=60):
    request = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    return urllib.request.urlopen(request, timeout=timeout)


def _asset_candidates():
    """产出 (版本, 资产名, 下载URL) 候选，先试 API，再退回已知版本。"""
    import json
    for prefix in DOWNLOAD_MIRRORS:
        try:
            with _get(prefix + RELEASE_API) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            for asset in data.get("assets", []):
                name = asset["name"].lower()
                if "windows" in name and ("x86_64" in name or "msvc" in name) and name.endswith(".zip"):
                    print("查到最新版本: {} -> {}".format(data.get("tag_name"), asset["name"]))
                    yield data.get("tag_name", "?"), asset["name"], asset["browser_download_url"]
            return
        except Exception as exc:
            print("  release API 经 {} 查询失败: {}".format(prefix or "直连", str(exc)[:80]))

    print("  API 不可达，改用已知版本清单")
    for tag, name in KNOWN_ASSETS:
        quoted = tag.replace("@", "%40")
        yield tag, name, "{}{}/{}".format(RELEASE_DL, quoted, name)


def install():
    os.makedirs(INSTALL_DIR, exist_ok=True)
    target = os.path.join(INSTALL_DIR, "tectonic.exe")

    if os.path.isfile(target):
        print("已存在: {}".format(target))
        return target

    for tag, name, url in _asset_candidates():
        payload = None
        for prefix in DOWNLOAD_MIRRORS:
            candidate = prefix + url
            label = prefix or "直连"
            try:
                print("  下载 {}（{}）...".format(name, label))
                with _get(candidate, timeout=300) as resp:
                    payload = resp.read()
                if payload[:2] != b"PK":
                    print("    返回的不是 zip（前两字节 {}），换源".format(payload[:2]))
                    payload = None
                    continue
                print("    成功，{:.1f} MB".format(len(payload) / 1e6))
                break
            except Exception as exc:
                print("    失败: {}".format(str(exc)[:120]))

        if payload is None:
            continue

        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            members = [m for m in archive.namelist() if m.lower().endswith("tectonic.exe")]
            if not members:
                raise RuntimeError("zip 内未找到 tectonic.exe，内容: {}".format(archive.namelist()[:10]))
            with archive.open(members[0]) as src, open(target, "wb") as dst:
                dst.write(src.read())

        print("安装完成: {}".format(target))
        return target

    raise RuntimeError("所有镜像与版本均下载失败")


def main():
    parser = argparse.ArgumentParser(description="安装 tectonic")
    parser.parse_args()

    try:
        install()
        print("\n下一步：pv_env 会把 {} 前置到 PATH，直接运行即可。".format(INSTALL_DIR))
        return 0
    except Exception as exc:
        print("\n安装失败: {}".format(exc))
        print("可手动下载（镜像）：")
        print("  https://ghproxy.net/https://github.com/tectonic-typesetting/tectonic/releases")
        print("解压 tectonic.exe 到 {}".format(INSTALL_DIR))
        return 1


if __name__ == "__main__":
    sys.exit(main())
