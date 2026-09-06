"""共享 fixtures：双节点（node-a/node-b）进程内集群。"""
from __future__ import annotations

import asyncio
import os
import socket
import threading
import time

import httpx
import pytest
import uvicorn

from fabric.central.app import create_app
from fabric.node.daemon import FabricNode


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


@pytest.fixture(scope="session")
def cluster(tmp_path_factory):
    from tests.test_handoff import MakeFileAdapter, ReadFileAdapter  # 延迟导入避免循环
    from fabric.node.adapters.echo import EchoAdapter

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
    # node-b 工作区预置 demo.txt：readfile 系测试自足，不依赖 handoff 链先行
    wb = tmp_path_factory.mktemp("ws-node-b")
    (wb / "demo.txt").write_text("HANDOFF-SENTINEL demo 内容", encoding="utf-8")
    for nid, adapters, ws in (("node-a", {"opencode": MakeFileAdapter(), "echo": EchoAdapter()},
                                tmp_path_factory.mktemp("ws-node-a")),
                              ("node-b", {"opencode": ReadFileAdapter(), "echo": EchoAdapter()}, wb)):
        async def _run(nid=nid, adapters=adapters):
            fn = FabricNode(node_id=nid, token="dev-token", url=f"ws://127.0.0.1:{port}",
                            adapters=adapters, workspace=str(ws))
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
