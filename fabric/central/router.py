"""命令路由（V0：确定性命令语法；自然语言意图路由 V1 再上，失败时回退到命令提示）。"""
from __future__ import annotations

from dataclasses import dataclass, field

KNOWN_HARNESSES = {"echo", "opencode", "dsh", "hermes"}


@dataclass
class Action:
    kind: str = "help"  # help / status / run / cancel / unknown
    node: str | None = None
    harness: str = "echo"
    goal: str = ""
    task_id: str | None = None
    raw: str = ""


def parse_command(text: str) -> Action:
    s = (text or "").strip()
    low = s.lower()
    a = Action(raw=s)
    if not s or low in ("help", "?", "？", "帮助"):
        a.kind = "help"
        return a
    if low in ("status", "状态"):
        a.kind = "status"
        return a
    if low in ("memory", "memory list", "记忆"):
        a.kind = "memory_list"
        return a
    if low.startswith("memory search ") or low.startswith("记忆搜索 "):
        a.kind = "memory_search"
        a.goal = s.split(None, 2)[2] if len(s.split(None, 2)) > 2 else ""
        return a
    if low.startswith("cancel ") or low.startswith("取消 "):
        a.kind = "cancel"
        a.task_id = s.split(None, 1)[1].strip()
        return a
    if low.startswith("run ") or low == "run":
        a.kind = "run"
        toks = s.split()[1:]
        while toks and (toks[0].startswith("@") or toks[0].lower() in KNOWN_HARNESSES):
            t0 = toks.pop(0)
            if t0.startswith("@"):
                a.node = t0[1:]
            else:
                a.harness = t0.lower()
        a.goal = " ".join(toks).strip()
        if not a.goal:
            a.kind = "help"
        return a
    a.kind = "unknown"
    return a


def help_text() -> str:
    return (
        "Agent Fabric 命令（V0）：\n"
        "  run [@节点] [harness] <任务>   例：run echo probe ／ run @mapian opencode probe ／ run @mapian echo !ls -la\n"
        "  status                        节点与任务状态\n"
        "  cancel <任务ID>               取消任务\n"
        "  memory list / memory search <关键词>\n"
        "（自然语言入口 V1 接入；V0 先用命令）"
    )
