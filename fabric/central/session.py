"""会话状态（V0.9 指挥会话）：单用户·单焦点·自然语言直通。

V1 语义（用户拍板 2026-09-06）：
  use <节点|别名> 选择主机（命令式，不做全自然语言路由）
  → 之后纯自然语言即任务：无运行任务→直接派发；有→排队为追加意见
  → 任务完成自动按追加意见续跑（复用 handoff resume 链）
  → 过程精简推送、结果完整推送（见 tasks.py）
约束：同一时刻只与一个 agent 会话（V2 再做多 agent 并行与消息自动归属）。
"""
from __future__ import annotations

import os

DEFAULT_ALIASES = {"麦片": "mapian", "米线": "test-node", "汤圆": "tangyuan",
                   "mapian": "mapian", "test-node": "test-node", "tangyuan": "tangyuan",
                   "本机": "test-node", "这台": "test-node"}


class SessionManager:
    def __init__(self, online_nodes):
        """online_nodes: () -> dict[node_id, info]（在线节点快照，用于校验/提示）"""
        self.online_nodes = online_nodes
        self.focus: str | None = None
        self.harness: str | None = None  # 会话当前 harness（use 自动挑，harness 命令可切）
        self.last_task: str | None = None
        self.followups: list[str] = []
        self._aliases = dict(DEFAULT_ALIASES)
        for pair in (os.getenv("AF_NODE_ALIASES") or "").split(","):
            if ":" in pair:
                k, v = pair.split(":", 1)
                self._aliases[k.strip()] = v.strip()

    def resolve(self, name: str) -> tuple[str | None, str]:
        """节点id直通（在线即认）→ 别名表兜底；返回 (node_id, 提示)。"""
        snap = self.online_nodes() or {}
        online = list(snap.keys()) if isinstance(snap, dict) else list(snap)  # 兼容两种形态
        raw = name.strip().lstrip("@")
        nid = raw if raw in online else None
        if nid is None:
            nid = self._aliases.get(raw) or self._aliases.get(raw.lower())
        if nid is not None and nid not in online:
            return None, f"{nid} 当前离线。在线：{', '.join(online) if online else '（无）'}"
        if nid is None:
            return None, f"不认识「{name}」。在线：{', '.join(online) if online else '（无）'}"
        return nid, ""

    def _node_info(self, nid: str) -> dict:
        """online_nodes() 兼容 list[str] 与 dict[nid, info] 两种回调形态。"""
        snap = self.online_nodes() or {}
        if isinstance(snap, dict):
            return snap.get(nid) or {}
        return {}  # list 形态只报在线性，无 harness 信息

    def pick_harness(self, nid: str) -> str | None:
        """节点在线 harness 里挑默认（echo 不算真 harness；稳定优先序 dsh>opencode>codex）"""
        hs = [h for h in (self._node_info(nid).get("harnesses") or []) if h != "echo"]
        for pref in ("dsh", "opencode", "codex"):
            if pref in hs:
                return pref
        return hs[0] if hs else None

    def node_harnesses(self, nid: str) -> list[str]:
        return [h for h in (self._node_info(nid).get("harnesses") or []) if h != "echo"]

    def switch(self, name: str) -> str:
        nid, err = self.resolve(name)
        if nid is None:
            return f"❌ {err}"
        old, self.focus = self.focus, nid
        self.followups.clear()  # 切换主机即换会话，旧焦点排队意见作废（V1 单会话语义）
        return (f"🎯 会话已切到 {nid}" + (f"（原 {old}）" if old and old != nid else "")
                + "，直接说任务即可；换主机用 use <麦片|米线>")

    def snapshot(self) -> str:
        queued = f"，排队意见 {len(self.followups)} 条" if self.followups else ""
        last = f"，最近任务 {self.last_task}" if self.last_task else ""
        return (f"焦点节点：{self.focus or '（未选择，use 麦片/米线）'}{last}{queued}")
