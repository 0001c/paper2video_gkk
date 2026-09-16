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

# API 不可达时的兜底：直接按已知版本构造下载地址。
# ⚠️ 必须用 **msvc** 构建。gnu 构建依赖 MinGW 运行库
# （libgcc_s_seh-1.dll / libwinpthread-1.dll / libstdc++-6.dll），
# 本机没有这些 DLL —— 实测 gnu 版连 `tectonic --version` 都跑不起来，
# 退出码 0xC0000135（STATUS_DLL_NOT_FOUND）。msvc 版是自包含的。
KNOWN_ASSETS = [
    ("tectonic@0.17.0", "tectonic-0.17.0-x86_64-pc-windows-msvc.zip"),
]


def _get(url, timeout=60):
    request = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    return urllib.request.urlopen(request, timeout=timeout)


def verify_tectonic(path):
    """运行 --version 判断是否真的可执行。

    只看「文件存在」是不够的 —— gnu 构建的 DLL 缺失在文件系统上完全看不出来，
    只有真正执行才会暴露。返回版本字符串，失败返回 None。
    """
    import subprocess
    try:
        result = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=90)
    except Exception as exc:  # noqa: BLE001
        print("    执行失败: {}: {}".format(type(exc).__name__, str(exc)[:120]))
        return None
    output = ((result.stdout or "") + (result.stderr or "")).strip()
    if result.returncode != 0:
        hint = ""
        if result.returncode in (3221225781, -1073741515):
            hint = "（0xC0000135 = 缺少 DLL，多半是下到了 gnu 构建）"
        print("    退出码 {}{} 输出={!r}".format(result.returncode, hint, output[:120]))
        return None
    return output or "unknown"


def _pick_windows_assets(assets):
    """从 release 资产里挑出 Windows x86_64 zip，msvc 优先、gnu 靠后。"""
    picked = []
    for asset in assets:
        name = asset["name"].lower()
        if "windows" not in name or not name.endswith(".zip"):
            continue
        if "x86_64" not in name and "msvc" not in name:
            continue
        # msvc 自包含，gnu 需要 MinGW DLL —— 排序保证先试 msvc
        rank = 0 if "msvc" in name else 1
        picked.append((rank, asset["name"], asset["browser_download_url"]))
    picked.sort(key=lambda item: item[0])
    return picked


def _asset_candidates():
    """产出 (版本, 资产名, 下载URL) 候选，先试 API，再退回已知版本。"""
    import json
    for prefix in DOWNLOAD_MIRRORS:
        try:
            with _get(prefix + RELEASE_API) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            found = _pick_windows_assets(data.get("assets", []))
            if found:
                for rank, name, url in found:
                    print("查到最新版本: {} -> {}{}".format(
                        data.get("tag_name"), name, "" if rank == 0 else "（gnu 备用）"))
                    yield data.get("tag_name", "?"), name, url
                return
        except Exception as exc:
            print("  release API 经 {} 查询失败: {}".format(prefix or "直连", str(exc)[:80]))

    print("  API 不可达，改用已知版本清单")
    for tag, name in KNOWN_ASSETS:
        quoted = tag.replace("@", "%40")
        yield tag, name, "{}{}/{}".format(RELEASE_DL, quoted, name)


def install(force=False):
    os.makedirs(INSTALL_DIR, exist_ok=True)
    target = os.path.join(INSTALL_DIR, "tectonic.exe")

    if os.path.isfile(target) and not force:
        print("已存在: {}".format(target))
        version = verify_tectonic(target)
        if version:
            print("  自检通过: {}".format(version))
            return target
        print("  自检未通过，重新安装")

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

        # 先落到临时名，验证通过再改名 —— 避免把一个跑不起来的 exe 留在目标位置
        staging = target + ".new"
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            members = [m for m in archive.namelist() if m.lower().endswith("tectonic.exe")]
            if not members:
                raise RuntimeError("zip 内未找到 tectonic.exe，内容: {}".format(archive.namelist()[:10]))
            with archive.open(members[0]) as src, open(staging, "wb") as dst:
                dst.write(src.read())

        print("  验证 {} ...".format(name))
        version = verify_tectonic(staging)
        if not version:
            print("  这个构建在本机不可用，换下一个候选")
            try:
                os.remove(staging)
            except OSError:
                pass
            continue

        if os.path.isfile(target):
            try:
                os.remove(target)
            except OSError:
                pass
        os.replace(staging, target)
        print("安装完成: {}（{}）".format(target, version))
        return target

    raise RuntimeError("所有镜像与版本均下载失败（或下载到的构建都跑不起来）")


def main():
    parser = argparse.ArgumentParser(description="安装 tectonic")
    parser.add_argument("--force", action="store_true", help="即使已存在也重新下载")
    parser.add_argument("--check", action="store_true", help="只验证现有 tectonic")
    args = parser.parse_args()

    target = os.path.join(INSTALL_DIR, "tectonic.exe")
    if args.check:
        if not os.path.isfile(target):
            print("未安装: {}".format(target))
            return 1
        version = verify_tectonic(target)
        print("tectonic: {}".format(version) if version else "tectonic 不可用")
        return 0 if version else 1

    try:
        install(force=args.force)
        print("\n下一步：pv_env 会把 {} 前置到 PATH，直接运行即可。".format(INSTALL_DIR))
        return 0
    except Exception as exc:
        print("\n安装失败: {}".format(exc))
        print("可手动下载（镜像）：")
        print("  https://ghproxy.net/https://github.com/tectonic-typesetting/tectonic/releases")
        print("请选 **msvc** 构建，解压 tectonic.exe 到 {}".format(INSTALL_DIR))
        return 1


if __name__ == "__main__":
    sys.exit(main())
