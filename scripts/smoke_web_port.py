"""端口预检的冒烟测试。

为什么需要它：Windows 与 POSIX 对 `SO_REUSEADDR` 的语义**正好相反**，
所以这个预检很容易"修好一个平台、弄坏另一个"，而两个平台的差异
在单一平台上跑一遍是看不出来的（第一次实现就踩了：Windows 上预检永远放行）。

因此这里分两层验证：
  1. **选项层**（平台无关，可在任一平台跑）：断言两个平台分支各自该设什么选项。
     这一层是本次的关键 —— 它把"平台差异"从运行期行为变成可断言的静态事实。
  2. **行为层**（依赖当前平台）：真实占端口，验证预检的三种场景。

用法：
    python scripts/smoke_web_port.py
"""
import os
import socket
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "web"))

PY = sys.executable
PORT = 8848


def _load_app():
    """导入 web/app.py（它不在 sys.path 里，用 importlib 按路径加载）。"""
    import importlib.util
    path = os.path.join(ROOT, "web", "app.py")
    spec = importlib.util.spec_from_file_location("p2v_web_app", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_option_layer(app):
    """断言两个平台的选项集合 —— 这是最容易写反的地方。"""
    print("=" * 64)
    print("[1] 选项层：两个平台分支各自该设什么 (与服务端语义对齐)")
    print("=" * 64)
    ok = True

    real = app._IS_WINDOWS

    socket_mod = socket
    try:
        app._IS_WINDOWS = True
        win = app._probe_options()
        names = [n for _, n, _ in win]
        has_reuse = socket_mod.SO_REUSEADDR in names
        has_excl = getattr(socket_mod, "SO_EXCLUSIVEADDRUSE", None) in names
        print("   Windows 分支选项 : %s" % names)
        print("     不含 SO_REUSEADDR      : %s  <- 含了就永远是'空闲'"
              % ("✔" if not has_reuse else "✘ 错误"))
        print("     含 SO_EXCLUSIVEADDRUSE : %s"
              % ("✔" if has_excl else "（该平台无此选项，跳过）"))
        ok &= not has_reuse

        app._IS_WINDOWS = False
        posix = app._probe_options()
        pnames = [n for _, n, _ in posix]
        print("   POSIX   分支选项 : %s" % pnames)
        print("     含 SO_REUSEADDR        : %s  <- 不含则 TIME_WAIT 会被误判成'被占用'"
              % ("✔" if socket_mod.SO_REUSEADDR in pnames else "✘ 错误"))
        ok &= socket_mod.SO_REUSEADDR in pnames
    finally:
        app._IS_WINDOWS = real

    print()
    print("   判定:", "通过" if ok else "失败")
    return ok


def test_behavior_layer(app):
    """真实占端口，验证预检行为（依赖当前平台）。"""
    print()
    print("=" * 64)
    print("[2] 行为层：预检判定 (当前平台: %s)" % ("Windows" if os.name == "nt" else "POSIX"))
    print("=" * 64)
    ok = True

    free_port = app._find_free_port("127.0.0.1", PORT + 100, tries=50)
    if free_port is None:
        print("   找不到空闲端口，跳过行为测试")
        return False
    print("   挑一个确认空闲的端口: %d" % free_port)
    if app._port_in_use("127.0.0.1", free_port):
        print("     ✘ 空闲端口被判成'占用'（假阳性 —— 会把能用的端口拦下来）")
        ok = False
    else:
        print("     ✔ 空闲端口判成可用")

    blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    blocker.bind(("127.0.0.1", free_port))
    blocker.listen(1)
    try:
        if app._port_in_use("127.0.0.1", free_port):
            print("     ✔ 被占端口判成不可用，占用者: %s" % (app._port_owner(free_port),))
        else:
            print("     ✘ 被占端口被判成可用（假阴性 —— 会一路走到启动才报 WinError）")
            ok = False
    finally:
        blocker.close()

    print()
    print("   判定:", "通过" if ok else "失败")
    return ok


def test_cli_paths(app):
    """端到端跑真实的启动命令，验证三种场景的对外行为。"""
    print()
    print("=" * 64)
    print("[3] 启动路径：真实跑 web/app.py")
    print("=" * 64)
    ok = True
    env = dict(os.environ)

    blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    blocker.bind(("127.0.0.1", PORT))
    blocker.listen(1)
    print("   已用假监听占住 %d" % PORT)

    # 场景 A：被占 + 未加 --auto-port -> 应退出码 2 且给出可操作提示
    r = subprocess.run([PY, os.path.join(ROOT, "web", "app.py")], cwd=ROOT, env=env,
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=120)
    out = r.stdout or ""
    actionable = ("已被占用" in out and "--port" in out and "--auto-port" in out)
    print("   A) 被占、无 --auto-port  -> 退出码 %s (期望 2) | 有可操作提示: %s"
          % (r.returncode, "是" if actionable else "否"))
    ok &= (r.returncode == 2 and actionable)

    # 场景 B：被占 + --auto-port -> 应顺延并成功监听
    proc = subprocess.Popen([PY, os.path.join(ROOT, "web", "app.py"), "--auto-port"],
                            cwd=ROOT, env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True,
                            encoding="utf-8", errors="replace")
    time.sleep(7)
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.settimeout(3)
    moved = probe.connect_ex(("127.0.0.1", PORT + 1)) == 0
    probe.close()
    proc.terminate()
    try:
        out_b, _ = proc.communicate(timeout=20)
    except Exception:
        proc.kill()
        out_b, _ = proc.communicate()
    print("   B) 被占、加 --auto-port -> 顺延到 %d 且可连通: %s"
          % (PORT + 1, "是" if moved else "否"))
    ok &= moved

    blocker.close()

    # 场景 C：空闲 -> 正常监听原端口
    proc = subprocess.Popen([PY, os.path.join(ROOT, "web", "app.py")], cwd=ROOT,
                            env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, encoding="utf-8", errors="replace")
    time.sleep(7)
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.settimeout(3)
    normal = probe.connect_ex(("127.0.0.1", PORT)) == 0
    probe.close()
    proc.terminate()
    try:
        out_c, _ = proc.communicate(timeout=20)
    except Exception:
        proc.kill()
        out_c, _ = proc.communicate()
    print("   C) 空闲                 -> 正常监听 %d: %s" % (PORT, "是" if normal else "否"))
    ok &= normal

    print()
    print("   判定:", "通过" if ok else "失败")
    return ok


def main():
    app = _load_app()
    results = [
        test_option_layer(app),
        test_behavior_layer(app),
        test_cli_paths(app),
    ]
    print()
    print("=" * 64)
    print("汇总: %s" % ("全部通过" if all(results) else "存在失败项"))
    print("=" * 64)
    print("注：POSIX 分支的**运行期**行为无法在 Windows 上真实复现，")
    print("    这里用'选项层'断言它该设的选项，迁到 Linux 后请重跑一次本脚本。")
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
