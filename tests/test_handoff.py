"""handoff（任务可续）测试：workspace 差分单测 + 双节点续跑 E2E。"""
from __future__ import annotations

import asyncio
import os
import socket
import threading
import time
from pathlib import Path

import httpx
import pytest
import uvicorn

from fabric.central.app import create_app
from fabric.node import workspace as WS
from fabric.node.daemon import FabricNode
from fabric.node.adapters.base import AdapterResult, RunContext


# ---------- 单测：snapshot / collect_changed / restore ----------

def test_workspace_diff_and_roundtrip(tmp_path: Path):
    root = tmp_path / "ws"
    root.mkdir()
    before = WS.snapshot(root)

    (root / "a.txt").write_text("hello", encoding="utf-8")
    (root / "sub").mkdir()
    (root / "sub" / "b.py").write_text("print(1)", encoding="utf-8")
    (root / "junk.pyc").write_bytes(b"\x00\x01")

    diff = WS.collect_changed(root, before)
    assert set(diff["files"]) == {"a.txt", "sub/b.py"}
    assert diff["files"]["a.txt"] == "hello"
    assert not diff["skipped"]

    # 恢复到另一个空工作区
    dst = tmp_path / "ws2"
    dst.mkdir()
    written = WS.restore(dst, diff["files"])
    assert sorted(written) == ["a.txt", "sub/b.py"]
    assert (dst / "sub" / "b.py").read_text(encoding="utf-8") == "print(1)"


def test_restore_rejects_path_escape(tmp_path: Path):
    root = tmp_path / "ws"
    root.mkdir()
    written = WS.restore(root, {"../evil.txt": "x", "/abs.txt": "y", "ok.txt": "z"})
    # ../ 被拒绝；绝对路径被就地包含为工作区内相对路径（无逃逸）
    assert sorted(written) == ["abs.txt", "ok.txt"]
    assert not (tmp_path / "evil.txt").exists()
    assert (root / "abs.txt").read_text(encoding="utf-8") == "y"


def test_collect_skips_oversize(tmp_path: Path):
    root = tmp_path / "ws"
    root.mkdir()
    before = WS.snapshot(root)
    (root / "big.txt").write_text("x" * (WS.MAX_FILE_BYTES + 1), encoding="utf-8")
    diff = WS.collect_changed(root, before)
    assert "big.txt" not in diff["files"]
    assert any("big.txt" in s for s in diff["skipped"])


# ---------- E2E：双节点跨工作区续跑 ----------

class MakeFileAdapter:
    """假 harness：在工作区写 demo.txt，输出一行确认。"""
    name = "makefile"

    async def start(self, goal, ctx: RunContext, progress):
        (ctx.workspace / "demo.txt").write_text("HANDOFF-SENTINEL\n" + goal, encoding="utf-8")
        await progress("demo.txt written")
        return AdapterResult(True, "文件已写入 demo.txt")

    async def cancel(self):
        pass


class ReadFileAdapter:
    """假 harness：读 demo.txt，把内容回显（文件不在则失败）。"""
    name = "readfile"

    async def start(self, goal, ctx: RunContext, progress):
        p = ctx.workspace / "demo.txt"
        if not p.exists():
            return AdapterResult(False, "demo.txt 不存在（handoff 恢复失败）")
        await progress("demo.txt found")
        return AdapterResult(True, p.read_text(encoding="utf-8"))

    async def cancel(self):
        pass


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


@pytest.fixture(scope="module")
def cluster(tmp_path_factory):
    port = free_port()
    db = tmp_path_factory.mktemp("db") / "fabric.db"
    saved = os.environ.get("AF_NODE_TOKENS")
    os.environ["AF_NODE_TOKENS"] = "node-a:dev-token,node-b:dev-token,test-node:dev-token"
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

    # 自定义 adapter 注册为 "opencode"（router 只认 KNOWN_HARNESSES；dict 键=harness 名）
    for nid, adapters in (("node-a", {"opencode": MakeFileAdapter()}),
                          ("node-b", {"opencode": ReadFileAdapter()})):
        async def _run(nid=nid, adapters=adapters):
            fn = FabricNode(node_id=nid, token="dev-token", url=f"ws://127.0.0.1:{port}",
                            adapters=adapters,
                            workspace=str(tmp_path_factory.mktemp(f"ws-{nid}")))
            await fn.run()
        threading.Thread(target=lambda: asyncio.run(_run()), daemon=True).start()

    with httpx.Client(timeout=10) as c:
        for _ in range(100):
            nodes = {n["node_id"]: n["status"] for n in c.get(f"http://127.0.0.1:{port}/api/nodes").json()["nodes"]}
            if nodes.get("node-a") == "online" and nodes.get("node-b") == "online":
                break
            time.sleep(0.15)
    yield port
    server.should_exit = True
    if saved is None:
        os.environ.pop("AF_NODE_TOKENS", None)
    else:
        os.environ["AF_NODE_TOKENS"] = saved


def _wait_task(c, task_id, timeout=15):
    end = time.time() + timeout
    while time.time() < end:
        cur = [t for t in c.get("/api/tasks").json()["tasks"] if t["id"] == task_id]
        if cur and cur[0]["status"] in ("done", "failed", "canceled"):
            return cur[0]
        time.sleep(0.2)
    pytest.fail(f"task {task_id} 未终态")


def test_cross_node_resume(cluster):
    port = cluster
    with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=15) as c:
        # 1) node-a 产出文件
        tid = c.post("/api/say", json={"text": "run @node-a opencode 制造第一个续跑锚点"}).json()["task_id"]
        t1 = _wait_task(c, tid)
        assert t1["status"] == "done"
        ho = (t1["result"] or {}).get("handoff") or {}
        assert "demo.txt" in ho.get("files", {}), f"handoff 应捕获 demo.txt: {ho}"

        # 2) resume 到 node-b（不同工作区），readfile 依赖恢复出的文件
        r = c.post("/api/say", json={"text": f"resume {tid} @node-b opencode"})
        tid2 = r.json()["task_id"]
        assert "恢复" in r.json()["reply"]
        t2 = _wait_task(c, tid2)
        assert t2["status"] == "done", t2["result"]
        assert "HANDOFF-SENTINEL" in t2["result"]["output"], "node-b 读到的应是 node-a 写入并恢复的内容"

        # 3) resume 链上的续跑任务自己也产 handoff
        ho2 = (t2["result"] or {}).get("handoff") or {}
        assert "demo.txt" in ho2.get("files", {})


def test_resume_missing_task(cluster):
    port = cluster
    with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=15) as c:
        reply = c.post("/api/say", json={"text": "resume T-nope99"}).json()["reply"]
        assert "不存在" in reply
