"""命令路由（V0：确定性命令语法；自然语言意图路由 V1 再上，失败时回退到命令提示）。"""
from __future__ import annotations

from dataclasses import dataclass, field

KNOWN_HARNESSES = {"echo", "opencode", "dsh", "hermes"}


@dataclass
class Action:
    kind: str = "help"  # help / status / run / cancel / resume / unknown
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
    if low.startswith("use ") or low.startswith("用 ") or low.startswith("切换 ") or low.startswith("切到 "):
        a.kind = "use"
        a.node = s.split(None, 1)[1].strip() if len(s.split(None, 1)) > 1 else ""
        return a
    if s.startswith("@") and len(s.split()) == 1:  # 裸 @节点 = 切换会话
        a.kind = "use"
        a.node = s[1:]
        return a
    if low.startswith("memory crystallize ") or low.startswith("经验结晶 "):
        a.kind = "memory_crystallize"
        a.goal = s.split(None, 2)[2]
        return a
    if low.startswith("memory forget ") or low.startswith("记忆删除 "):
        a.kind = "memory_forget"
        a.goal = s.split(None, 2)[2] if len(s.split(None, 2)) > 2 else ""
        return a
    if low == "dream" or low == "记忆整理":
        a.kind = "dream"
        return a
    if low.startswith("memory add ") or low.startswith("记忆添加 "):
        a.kind = "memory_add"
        parts = s.split(None, 3)  # memory add <kind> <text...>
        a.harness = parts[2].lower() if len(parts) > 3 else ""   # 复用字段存 kind
        a.goal = parts[3] if len(parts) > 3 else ""
        return a
    if low.startswith("memory search ") or low.startswith("记忆搜索 "):
        a.kind = "memory_search"
        a.goal = s.split(None, 2)[2] if len(s.split(None, 2)) > 2 else ""
        return a
    if low.startswith("memory") or low.startswith("记忆"):
        a.kind = "memory_list"
        return a
    if low.startswith("task ") and s.split(None, 1)[1].strip().upper().startswith("T-"):
        a.kind = "task_detail"
        a.task_id = s.split(None, 1)[1].strip().split()[0]
        return a
    if low == "tasks" or low == "任务" or low.startswith("tasks ") or low.startswith("任务 "):
        a.kind = "tasks_list"
        toks = s.split()
        a.harness = toks[1] if len(toks) > 1 else ""   # 复用存 N
        return a
    if low == "skills" or low == "技能" or low.startswith("skills ") or low.startswith("技能 "):
        a.kind = "skills_list"
        return a
    if low == "memories" or low == "记忆库" or low.startswith("memories ") or low.startswith("记忆库 "):
        a.kind = "memories_list"
        toks = s.split()
        a.harness = toks[1] if len(toks) > 1 else ""
        return a
    if low == "candidates" or low == "候选" or low.startswith("candidates ") or low.startswith("候选 "):
        a.kind = "candidates_list"
        return a
    if low.startswith("review ") or low.startswith("审核 "):
        a.kind = "review"
        toks = s.split()
        if len(toks) >= 3 and toks[1].lower() in ("all", "全部"):
            a.task_id = "all"
            a.harness = toks[2].lower()          # 动作 promote/discard
        elif len(toks) >= 3 and toks[1].upper().startswith(("MC-", "M-")):
            a.task_id = toks[1]
            a.harness = toks[2].lower()          # 动作
        return a
    if low.startswith("cancel ") or low.startswith("取消 "):
        a.kind = "cancel"
        a.task_id = s.split(None, 1)[1].strip()
        return a
    if low.startswith("resume ") or low.startswith("续跑 "):
        a.kind = "resume"
        toks = s.split()[1:]
        if toks and toks[0].upper().startswith("T-"):
            a.task_id = toks.pop(0)
        while toks and (toks[0].startswith("@") or toks[0].lower() in KNOWN_HARNESSES):
            t0 = toks.pop(0)
            if t0.startswith("@"):
                a.node = t0[1:]
            else:
                a.harness = t0.lower()
        a.goal = " ".join(toks).strip()  # 可选追加指示
        if not a.task_id:
            a.kind = "help"
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
        "Agent Fabric 命令（V0.5）：\n"
        "  run [@节点] [harness] <任务>    例：run echo probe ／ run @mapian opencode 写个fib.py并运行\n"
        "  resume <任务ID> [@节点] [harness] [追加指示]\n"
        "                                 跨节点续跑：恢复上一轮工作区文件+前情提要，例：resume T-abc123 @test-node\n"
        "  status                         节点与任务状态\n"
        "  cancel <任务ID>                取消任务\n"
        "  memory / memory search <关键词>\n"
        "  memory add <project|skill|fact|lesson|decision> <内容>   人工注入中央记忆（即审即入）\n"
        "  memory crystallize <主题>   经验聚类→节点模型蒸馏→结晶为技能（≥2条相关经验）\n"
        "  memory forget <id> / dream   删除记忆 / 整理（去水合并+流水限额）\n"
        "  candidates / review <id|all> <promote|discard>   经验候选审核（promote→experience入库）\n"
        "  tasks [N] / task <T-id> / memories [N] / skills   管理面板\n"
    )
