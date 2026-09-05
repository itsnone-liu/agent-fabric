"""fabric-node 守护进程：出站 WebSocket 连中央（家庭/机房节点无需公网 IP）。

模式对齐 Syne remote node：主动出站、指数退避重连（2s→60s，睡醒自动恢复）、
心跳保活、本地 outbox 抗断网（PLAN §5/§23）。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import socket
import sys
import time
from pathlib import Path

import websockets

from ..shared import protocol as P
from .adapters.base import RunContext
from .adapters.echo import EchoAdapter
from .adapters.opencode import OpenCodeAdapter
from .outbox import Outbox

HEARTBEAT_INTERVAL = 15.0
RECONNECT_MIN, RECONNECT_MAX = 2.0, 60.0


class FabricNode:
    def __init__(self, node_id: str, token: str, url: str,
                 adapters: dict | None = None, workspace: str | None = None,
                 allow_shell: bool = False, outbox_path: str | None = None):
        self.node_id = node_id
        self.token = token
        self.url = url.rstrip("/")
        self.adapters = adapters or {"echo": EchoAdapter(), "opencode": OpenCodeAdapter()}
        self.workspace = Path(workspace or os.getenv("AF_WORKSPACE") or os.getcwd()).resolve()
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.allow_shell = allow_shell
        default_outbox = Path.home() / ".fabric-node" / f"{node_id}-outbox.db"
        self.outbox = Outbox(outbox_path or default_outbox)
        self._cancel: set[str] = set()

    # ---------- 主循环 ----------
    async def run(self):
        backoff = RECONNECT_MIN
        while True:
            uri = f"{self.url}/ws/node/{self.node_id}?token={self.token}"
            try:
                async with websockets.connect(uri, open_timeout=15, ping_interval=20, max_size=8 * 1024 * 1024) as ws:
                    print(f"[fabric-node] 已连接 {self.url}（{self.node_id}，harnesses={sorted(self.adapters)}）", flush=True)
                    await self._hello(ws)
                    await self._flush_outbox(ws)
                    backoff = RECONNECT_MIN
                    hb = asyncio.create_task(self._heartbeat(ws))
                    try:
                        await self._recv_loop(ws)
                    finally:
                        hb.cancel()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                pending = self.outbox.size()
                print(f"[fabric-node] 断开：{e!r}；{backoff:.0f}s 后重连（outbox 积压 {pending} 条）", flush=True)
            await asyncio.sleep(backoff)
            backoff = min(RECONNECT_MAX, backoff * 2)

    async def _hello(self, ws):
        info = {
            "node_id": self.node_id,
            "hostname": socket.gethostname(),
            "os": platform.platform(),
            "python": platform.python_version(),
            "harnesses": sorted(self.adapters),
            "capabilities": {"shell": self.allow_shell},
            "workspace": str(self.workspace),
        }
        await ws.send(json.dumps(P.make(P.T_NODE_ONLINE, info, node_id=self.node_id)))

    async def _flush_outbox(self, ws):
        async def _send(env):
            await ws.send(json.dumps(env))
        n = await self.outbox.drain(_send)
        if n:
            print(f"[fabric-node] outbox 重放 {n} 条", flush=True)

    async def _heartbeat(self, ws):
        while True:
            await asyncio.sleep(HEARTBEAT_INTERVAL)
            try:
                await ws.send(json.dumps(P.make(P.T_HEARTBEAT, node_id=self.node_id)))
            except Exception:
                return

    # ---------- 收命令 ----------
    async def _recv_loop(self, ws):
        async for raw in ws:
            env = json.loads(raw)
            t = env.get("type", "")
            if t == P.T_TASK_START:
                asyncio.create_task(self._run_task(env, ws))
            elif t == P.T_TASK_CANCEL:
                tid = env.get("task_id")
                self._cancel.add(tid)
                for a in self.adapters.values():
                    try:
                        await a.cancel()
                    except Exception:
                        pass
            elif t == P.T_TASK_MESSAGE:
                pass  # V1：转发给运行中 harness 会话
            elif t == P.T_NODE_CONFIGURE:
                pass  # V1：热更新节点配置

    # ---------- 执行任务 ----------
    async def _send(self, ws, env: dict):
        try:
            await ws.send(json.dumps(env))
        except Exception:
            try:
                self.outbox.enqueue(env)
            except Exception as e:
                print(f"[fabric-node] outbox 入队失败: {e!r}", flush=True)

    async def _run_task(self, env: dict, ws):
        pl = env.get("payload", {})
        tid = pl.get("task_id") or env.get("task_id")
        harness = pl.get("harness", "echo")
        goal = pl.get("goal", "")

        await self._send(ws, P.make(P.T_TASK_ACCEPTED, {"task_id": tid}, task_id=tid, node_id=self.node_id))
        adapter = self.adapters.get(harness)
        if adapter is None:
            await self._send(ws, P.make(P.T_TASK_FAILED,
                                        {"task_id": tid, "error": f"节点未安装 harness: {harness}（可用: {sorted(self.adapters)}）"},
                                        task_id=tid, node_id=self.node_id))
            return
        await self._send(ws, P.make(P.T_TASK_RUNNING, {"task_id": tid}, task_id=tid, node_id=self.node_id))

        async def progress(text: str):
            await self._send(ws, P.make(P.T_TASK_PROGRESS, {"task_id": tid, "text": str(text)[:1000]},
                                        task_id=tid, node_id=self.node_id))

        try:
            ctx = RunContext(workspace=self.workspace, allow_shell=self.allow_shell)
            res = await adapter.start(goal, ctx, progress)
            canceled = tid in self._cancel
            await self._send(ws, P.make(P.T_TASK_RESULT,
                                        {"task_id": tid, "ok": res.ok, "output": res.output[:20000],
                                         "artifacts": res.artifacts,
                                         **({"canceled": True} if canceled else {})},
                                        task_id=tid, node_id=self.node_id))
            # 任务完成后提交经验候选（PLAN §18：节点只交 candidate，入库由中央审）
            tail = res.output[-200:].replace("\n", " ")
            self._emit_memory_candidate(ws, harness, goal, res.ok, tid, tail)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            await self._send(ws, P.make(P.T_TASK_FAILED, {"task_id": tid, "error": repr(e)[:2000]},
                                        task_id=tid, node_id=self.node_id))
        finally:
            self._cancel.discard(tid)

    def _emit_memory_candidate(self, ws, harness: str, goal: str, ok: bool, tid: str, tail: str):
        content = f"[{harness}] goal={goal[:120]} → {'ok' if ok else 'failed'} @ {self.node_id}; tail: {tail}"
        env = P.make(P.T_MEMORY_CANDIDATE,
                     {"kind": "experience", "content": content, "source_task": tid},
                     task_id=tid, node_id=self.node_id)
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self._send(ws, env))
        except RuntimeError:
            pass


def main():
    ap = argparse.ArgumentParser(description="Agent Fabric Node")
    ap.add_argument("--node-id", default=os.getenv("AF_NODE_ID", "test-node"))
    ap.add_argument("--token", default=os.getenv("AF_NODE_TOKEN", "dev-token"))
    ap.add_argument("--central-url", default=os.getenv("AF_CENTRAL_URL", "ws://127.0.0.1:8000"))
    ap.add_argument("--workspace", default=os.getenv("AF_WORKSPACE", ""))
    ap.add_argument("--allow-shell", action="store_true",
                    default=os.getenv("AF_ALLOW_SHELL") == "1")
    a = ap.parse_args()
    node = FabricNode(a.node_id, a.token, a.central_url, workspace=a.workspace, allow_shell=a.allow_shell)
    try:
        asyncio.run(node.run())
    except KeyboardInterrupt:
        print("[fabric-node] 退出", flush=True)
        sys.exit(0)


if __name__ == "__main__":
    main()
