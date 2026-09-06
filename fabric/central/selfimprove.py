"""自我完善机制（V0.12）：系统用自己的管道审计自己。

用户 2026-09-06 拍板的边界：
  · 消耗可控 —— 审计任务走节点免费模型（internal，零 LLM key）
  · 只建议不自动改码 —— 报告推送给人，人拍板才动手
    （免费模型质量不够给"自动改自己"背锅；建议→人审→执行）
触发：`improve` 命令（人）或 fabric-improve.timer（每日）POST /api/improve。
"""
from __future__ import annotations

import subprocess
import time


def _sh(cmd: str, cwd: str | None = None, timeout: int = 30) -> str:
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True,
                           cwd=cwd, timeout=timeout)
        return (r.stdout or r.stderr or "").strip()
    except Exception as e:
        return f"(unavailable: {e!r})"


def collect_context(repo: str = ".", db_stats: dict | None = None) -> str:
    """收集审计素材：代码活动、测试健康、记忆健康（全部确定性，零 LLM）。"""
    parts = [
        "== 最近 15 条提交 ==",
        _sh("git log --oneline -15", cwd=repo),
        "== 代码规模 ==",
        _sh("find fabric -name '*.py' | xargs wc -l | tail -1; ls tests/*.py | wc -l", cwd=repo),
        "== 测试（快速）==",
        _sh(".venv/bin/python -m pytest tests/ -q --timeout=60 2>&1 | tail -3 || "
            ".venv/bin/python -m pytest tests/ -q 2>&1 | tail -3", cwd=repo, timeout=90),
        "== TODO/FIXME/坑 ==",
        _sh("grep -rn 'TODO\\|FIXME\\|V1：\\|V2：' fabric/ --include='*.py' | head -15", cwd=repo),
        "== 记忆库健康 ==",
        str(db_stats or {}),
        "== TESTING.md 最近一轮 ==",
        _sh("tail -30 TESTING.md", cwd=repo),
    ]
    return "\n".join(parts)[:6000]


AUDIT_GOAL_TMPL = """你是 agent-fabric 项目的审计员。以下是一个多节点 agent 编排系统的自检数据。
请输出一份自我完善报告，格式严格如下：

【体检结论】一句话：系统当前最值得投入的 1 个问题
【建议】按性价比排序的 3 条改进（每条：做什么/为什么值得/预估工作量小中大）
【风险】近期改动可能引入的 1 个风险点
最后单独一行 [LESSON] 无

只基于下面数据，不要臆造；数据里没有的信息就说"数据未覆盖"。

{context}"""


async def run_audit(tm, memory, node: str | None = None, repo: str = ".") -> dict:
    """收集→组 goal→派 internal 审计任务→等结果。返回 {"ok", "report"}。"""
    import asyncio as _a
    from .tasks import TERMINAL
    stats = {}
    try:
        rows = memory.store.all_memories(1000)
        stats = {"memories": len(rows),
                 "by_kind": {k: sum(1 for m in rows if m["kind"] == k)
                             for k in {m["kind"] for m in rows}},
                 "no_hits_pct": round(100 * sum(1 for m in rows if not m.get("hits")) / max(1, len(rows)))}
    except Exception:
        pass
    goal = AUDIT_GOAL_TMPL.format(context=collect_context(repo, stats))
    task = tm.create(goal[:12000], "opencode", node, internal=True)
    await tm.dispatch(task)
    t0 = time.time()
    while time.time() - t0 < 480:  # 免费模型慢，8 分钟兜底
        await _a.sleep(4)
        t = tm.store.get_task(task["id"])
        if t and t.get("status") in TERMINAL:
            if t["status"] != "done":
                return {"ok": False, "hint": f"审计任务 {t['status']}"}
            from .reply_clean import extract_reply
            report = extract_reply(str((t.get("result") or {}).get("output") or ""))
            return {"ok": bool(report), "report": report or "（审计输出为空）",
                    "task_id": task["id"]}
    return {"ok": False, "hint": "审计任务超时"}
