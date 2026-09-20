"""后端冒烟测试：真启动一次 server，打真实 HTTP 请求。

覆盖的是「装完能不能用」这一层，不是业务逻辑：
  1. 进程能起来、端口能监听、/api/health 返回 ok
  2. GET / 真能取到单文件看板（静态文件服务与路径白名单没写错）
  3. 未知静态路径 → 404，未知 /api/ 路径 → 404 JSON（不是 500 或 HTML 报错页）
  4. 空库下 /api/clients、/api/taxonomy 能正常返回（首次使用不炸）

**不需要 LLM 凭据**：所有断言都不依赖真实模型调用，所以能在 CI 里跑。
（老 e2e_test.py 需要凭据 + 真实 LLM 调用，属于人工验收，不在这里。）

运行：
    PYTHONPATH=src .venv/bin/python tests/test_server_smoke.py
或由 pytest 收集执行。
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"


# ----------------------------------------------------------------- 工具


def _free_port() -> int:
    """向系统要一个空闲端口，避免和本机已在跑的服务撞车。"""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _request(base: str, path: str, timeout: float = 10.0, method: str = "GET",
             payload: dict | None = None):
    """返回 (status, body_bytes)。HTTP 错误也返回状态码，不抛异常。"""
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(base + path, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:  # 404 等属于被测行为，不是测试错误
        return e.code, e.read()


def _wait_ready(base: str, timeout: float = 30.0) -> None:
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        try:
            status, body = _request(base, "/api/health", timeout=3)
            if status == 200 and json.loads(body.decode()).get("ok"):
                return
            last = f"status={status}"
        except Exception as exc:  # noqa: BLE001  进程还没起来，继续等
            last = repr(exc)
        time.sleep(0.3)
    raise AssertionError(f"server 未在 {timeout}s 内就绪（最后状态：{last}）")


class _Server:
    """上下文管理器：拉起 server 子进程，退出时一定收尸。"""

    def __init__(self) -> None:
        self.port = _free_port()
        self.base = f"http://127.0.0.1:{self.port}"
        self.proc: subprocess.Popen | None = None

    def __enter__(self) -> _Server:
        env = dict(os.environ)
        env["PYTHONPATH"] = str(SRC)
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "salesvoice.server", str(self.port)],
            cwd=str(ROOT),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        try:
            _wait_ready(self.base)
        except Exception:
            self.__exit__()
            raise
        return self

    def __exit__(self, *exc) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=10)


# ----------------------------------------------------------------- 断言


def run_checks(make_server=_Server) -> int:
    checks: list[tuple[str, object]] = []  # (说明, 失败时抛出的异常 or None)

    with make_server() as srv:
        # 1) 健康检查
        status, body = _request(srv.base, "/api/health")
        health = json.loads(body.decode())
        checks.append((f"/api/health 200 (got {status})", None if status == 200 else AssertionError("非 200")))
        checks.append(("/api/health ok=true", None if health.get("ok") else AssertionError(str(health))))

        # 2) 单文件看板真的能被取到
        status, body = _request(srv.base, "/")
        html = body.decode("utf-8", "replace")
        checks.append((f"GET / 200 (got {status})", None if status == 200 else AssertionError("非 200")))
        checks.append(
            ("看板 HTML 含标题 SalesVoice",
             None if "SalesVoice" in html else AssertionError(html[:200])))

        # 3) 404 语义正确
        status, body = _request(srv.base, "/no-such-file.png")
        checks.append((f"未知静态资源 404 (got {status})", None if status == 404 else AssertionError("期望 404")))
        status, body = _request(srv.base, "/api/no-such-endpoint")
        ok404 = status == 404
        payload = {}
        if ok404:
            try:
                payload = json.loads(body.decode())
            except Exception:  # noqa: BLE001
                ok404 = False
        checks.append((f"未知 API 返回 404 JSON (got {status})",
                       None if ok404 and "error" in payload else AssertionError(body[:200])))

        # 4) 空库不炸
        for path, key in (("/api/clients", "clients"), ("/api/taxonomy", "categories")):
            status, body = _request(srv.base, path)
            good = status == 200
            if good:
                try:
                    good = key in json.loads(body.decode())
                except Exception:  # noqa: BLE001
                    good = False
            checks.append((f"{path} 返回含 {key!r} (got {status})",
                           None if good else AssertionError(body[:200])))

        # 5) Notion 集成：状态接口可用；未配置凭据时同步请求必须被明确拒绝（不能 500）
        status, body = _request(srv.base, "/api/notion")
        st = json.loads(body.decode()) if status == 200 else {}
        checks.append((f"/api/notion 200 且含 has_key/ledger (got {status})",
                       None if status == 200 and "has_key" in st and "ledger" in st
                       else AssertionError(body[:200])))

        status, body = _request(srv.base, "/api/notion/sync", method="POST",
                                payload={})
        try:
            started = json.loads(body.decode())
        except Exception:  # noqa: BLE001
            started = {}
        if started.get("ok"):
            # 配了 token + 投放区的环境：请求被接受（后台线程开跑）即可
            checks.append(("POST /api/notion/sync 已接受（后台执行）", None))
        else:
            good = status == 200 and bool(started.get("error"))
            checks.append((f"未配置时 POST /api/notion/sync 给出明确原因 (got {status})",
                           None if good else AssertionError(body[:200])))

    failed = 0
    print("=" * 76)
    print("后端冒烟测试")
    print("=" * 76)
    for desc, err in checks:
        if err is None:
            print(f"✓ {desc}")
        else:
            failed += 1
            print(f"✗ {desc}\n     ← {err}")
    print("-" * 76)
    print(f"通过 {len(checks) - failed} / {len(checks)}")
    print("=" * 76)
    return 0 if failed == 0 else 1


def test_server_smoke() -> None:
    """pytest 入口。"""
    assert run_checks() == 0


if __name__ == "__main__":
    sys.exit(run_checks())
