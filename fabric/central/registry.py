"""节点注册表：在线状态、能力、心跳过期（节点层模式对齐 Syne 的 node registry/pairing/heartbeat）。"""
from __future__ import annotations

import time

STALE_AFTER = 45.0  # 3 个心跳周期（15s）无心跳判离线


class NodeState:
    def __init__(self, node_id: str, info: dict, ws):
        self.node_id = node_id
        self.info = info or {}
        self.ws = ws
        self.last_seen = time.time()
        self.status = "online"
        self.connected_at = time.time()

    @property
    def harnesses(self) -> list[str]:
        return list(self.info.get("harnesses") or [])

    def touch(self):
        self.last_seen = time.time()

    def snapshot(self) -> dict:
        return {
            "node_id": self.node_id,
            "status": self.status,
            "last_seen": self.last_seen,
            "connected_at": self.connected_at,
            "info": self.info,
        }


class NodeRegistry:
    def __init__(self):
        self.nodes: dict[str, NodeState] = {}

    def register(self, node_id: str, info: dict, ws) -> NodeState:
        ns = NodeState(node_id, info, ws)
        self.nodes[node_id] = ns
        return ns

    def unregister(self, node_id: str, expected: NodeState | None = None):
        ns = self.nodes.get(node_id)
        # 新连接已顶替旧连接时，不误删新状态
        if ns is not None and (expected is None or ns is expected):
            ns.status = "offline"

    def touch(self, node_id: str):
        ns = self.nodes.get(node_id)
        if ns:
            ns.touch()

    def sweep(self):
        now = time.time()
        for ns in self.nodes.values():
            if ns.status == "online" and now - ns.last_seen > STALE_AFTER:
                ns.status = "offline"

    def online(self) -> list[NodeState]:
        return [ns for ns in self.nodes.values() if ns.status == "online"]

    def snapshot(self) -> list[dict]:
        return [ns.snapshot() for ns in self.nodes.values()]

    def pick(self, harness: str | None = None, node_id: str | None = None) -> NodeState | None:
        """V0 规则化调度（PLAN §11）：显式指定 > 能力匹配 > 在线即可，暂不引入 LLM。"""
        cands = self.online()
        if harness:
            cands = [ns for ns in cands if harness in ns.harnesses]
        if node_id:
            cands = [ns for ns in cands if ns.node_id == node_id]
        if not cands:
            return None

        def score(ns: NodeState) -> int:
            s = 0
            if node_id and ns.node_id == node_id:
                s += 100
            if harness and harness in ns.harnesses:
                s += 30
            s += 10  # 在线基础分
            return s

        return sorted(cands, key=score, reverse=True)[0]
