"""本地端到端垂直切片：channel(REST) → central → node(echo/opencode adapter) → 结果回流 → 记忆候选。

不依赖外部网络与服务：central 起在随机端口（线程内 uvicorn），node 以线程内 asyncio 连入。
"""
from __future__ import annotations

import asyncio
import socket
import threading
import time

import httpx
import pytest
import uvicorn

from fabric.central.app import create_app
from fabric.node.daemon import FabricNode
from fabric.node.adapters.echo import EchoAdapter
from fabric.node.adapters.opencode import OpenCodeAdapter


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


@pytest.fixture(scope="module")
def central(tmp_path_factory):
    port = free_port()
    db = tmp_path_factory.mktemp("db") / "fabric.db"
    server = uvicorn.Server(uvicorn.Config(create_app(db_path=str(db)),
                                           host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    with httpx.Client(timeout=10) as c:
        for _ in range(100):
            try:
                c.get(f"http://127.0.0.1:{port}/api/health")
                break
            except Exception:
                time.sleep(0.1)
    yield port
    server.should_exit = True


@pytest.fixture(scope="module")
def node(central, tmp_path_factory):
    port = central

    async def _run():
        fn = FabricNode(
            node_id="test-node", token="dev-token", url=f"ws://127.0.0.1:{port}",
            adapters={"echo": EchoAdapter(), "opencode": OpenCodeAdapter()},
            workspace=str(tmp_path_factory.mktemp("ws")))
        await fn.run()

    threading.Thread(target=lambda: asyncio.run(_run()), daemon=True).start()
    with httpx.Client(timeout=10) as c:
        for _ in range(100):
            try:
                nodes = c.get(f"http://127.0.0.1:{port}/api/nodes").json()["nodes"]
                if any(n["node_id"] == "test-node" and n["status"] == "online" for n in nodes):
                    break
            except Exception:
                pass
            time.sleep(0.15)
    yield port


def _client(port):
    return httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=15)


def _wait_task(c, task_id, timeout=20):
    end = time.time() + timeout
    while time.time() < end:
        tasks = c.get("/api/tasks").json()["tasks"]
        cur = [t for t in tasks if t["id"] == task_id]
        if cur and cur[0]["status"] in ("done", "failed", "canceled"):
            return cur[0]
        time.sleep(0.2)
    pytest.fail(f"task {task_id} 未在 {timeout}s 内终态")


def test_e2e_echo_run(node):
    port = node
    with _client(port) as c:
        r = c.post("/api/say", json={"text": "run echo hello fabric"})
        assert r.status_code == 200
        tid = r.json()["task_id"]
        t = _wait_task(c, tid)
        assert t["status"] == "done"
        assert "hello fabric" in t["result"]["output"]
        assert t["node_id"] == "test-node"


def test_e2e_progress_events_streamed(node):
    port = node
    with _client(port) as c:
        tid = c.post("/api/say", json={"text": "run echo probe"}).json()["task_id"]
        t = _wait_task(c, tid)
        assert t["status"] == "done"
        evs = c.get(f"/api/events", params={"task_id": tid, "limit": 50}).json()["events"]
        types = [e["type"] for e in evs]
        assert "task.progress" in types
        assert "task.result" in types


def test_shell_disabled_by_default(node):
    port = node
    with _client(port) as c:
        tid = c.post("/api/say", json={"text": "run echo !echo pwned"}).json()["task_id"]
        t = _wait_task(c, tid)
        assert t["status"] == "failed"
        assert "AF_ALLOW_SHELL" in t["result"]["output"]


def test_unknown_harness_no_node(node):
    port = node
    with _client(port) as c:
        reply = c.post("/api/say", json={"text": "run hermes do something"}).json()["reply"]
        assert "派发失败" in reply or "没有在线节点" in reply


def test_memory_candidate_pipeline(node):
    port = node
    with _client(port) as c:
        tid = c.post("/api/say", json={"text": "run echo 记忆流水线验证任务"}).json()["task_id"]
        _wait_task(c, tid)
        deadline = time.time() + 5
        found = None
        while time.time() < deadline and not found:
            cands = c.get("/api/memory/candidates").json()["candidates"]
            found = next((x for x in cands if x.get("source_task") == tid), None)
            time.sleep(0.2)
        assert found, "任务完成后应产生 memory candidate"
        # promote → 可检索
        r = c.post("/api/memory/review", json={"id": found["id"], "action": "promote",
                                               "kind": "experience", "importance": 2})
        assert r.json().get("status") == "promoted"
        res = c.get("/api/memory", params={"q": "记忆流水线"}).json()["results"]
        assert any("记忆流水线" in m["content"] for m in res)
        pkg_note = c.get("/api/tasks").json()["tasks"][0]
        assert pkg_note  # context package 已随 task.start 下发（见 events）


def test_status_and_help(node):
    port = node
    with _client(port) as c:
        assert "test-node" in c.post("/api/say", json={"text": "status"}).json()["reply"]
        # V0.9：未知文本不再兜底 help，而是进入会话引导（选主机后即自然语言任务）
        assert "use" in c.post("/api/say", json={"text": "不知道说什么"}).json()["reply"]
