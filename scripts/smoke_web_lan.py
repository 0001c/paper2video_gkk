"""局域网访问边界冒烟：不启动 worker、不调用模型、不修改任务。"""
import importlib.util
from pathlib import Path

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("lan_smoke_app", ROOT / "web/app.py")
web = importlib.util.module_from_spec(spec)
spec.loader.exec_module(web)


def main():
    for invalid in ("0.0.0.0/0", "8.8.8.0/24", "127.0.0.0/8", "::/0", "bad-subnet"):
        try:
            web._lan_network(invalid)
        except ValueError:
            pass
        else:
            raise AssertionError(f"危险或无效子网被接受：{invalid}")
    assert str(web._lan_network("192.168.31.128/24")) == "192.168.31.0/24"
    base = "http://192.168.31.128:8848"
    local = TestClient(web.app, base_url="http://127.0.0.1:8848", client=("127.0.0.1", 50000))
    lan = TestClient(web.app, base_url=base, client=("192.168.31.20", 50000))
    # 空请求仅触发字段校验；无生成调用、无任务写入。
    assert local.post("/api/course/plan", json={}).status_code == 422
    assert lan.post("/api/course/plan", json={}).status_code == 503
    web.app.state.lan_networks = (web._lan_network("192.168.31.0/24"),)
    assert lan.get("/").status_code == 200
    assert lan.post("/api/course/plan", json={}, headers={"Origin": base}).status_code == 422
    assert lan.post("/api/course/plan", json={}, headers={"Origin": "https://evil.example"}).status_code == 403
    assert lan.post("/api/course/plan", json={}, headers={"Host": "evil.example", "Origin": "http://evil.example"}).status_code == 403
    assert local.post("/api/course/plan", json={}).status_code == 422
    for remote in ("192.168.32.20", "8.8.8.8", "::ffff:192.168.31.20"):
        outside = TestClient(web.app, base_url=base, client=(remote, 50000))
        assert outside.get("/", headers={"X-Forwarded-For": "192.168.31.20"}).status_code == 403
    print("PASS 局域网读取与同源写入；默认本机限制、子网边界、跨站、恶意 Host 和伪造转发地址检查通过")


if __name__ == "__main__":
    main()
