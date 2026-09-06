"""中央记忆管理（V0.6：三级记忆 + 检索预算）。

生命周期对齐 Mem0 的 triage→recall→dream 与 PLAN §18：
  节点 memory.candidate → pending（capture/triage）
  → 人工 review promote（成为 memory）或 discard
  → 检索组装 context_package（recall，按层级预算截断）
  → consolidate（dream）留待 V1（合并重复/冲突、置信度衰减）

三级记忆（PLAN §12）：
  task    机器自动：每个任务完成即记（goal+尾部+节点+成败），resume 链优先召回
  project 人工注入：项目约定/决策（memory add project ...）
  skill   人工沉淀：可复用操作程序（memory add skill ...）
  experience 节点回流候选 promote（原有）
Scope 命名对齐 Agent Memory OS：private:user / agent:primary / project:xxx / global。
V0 检索 = 关键词命中 + importance 加权（确定性，零 token）；
PG+pgvector 迁移后换 向量 + BM25 融合。
"""
from __future__ import annotations

from .store import Store

# Context Package 预算（PLAN §20：恒定预算，不随历史膨胀；V0 按≈4字符/token折算）
BUDGET = {"identity": 200, "task": 400, "project": 800, "experience": 600, "skill": 600}

MANUAL_KINDS = {"project", "skill", "fact", "lesson", "decision", "preference", "experience"}


class MemoryManager:
    def __init__(self, store: Store, identity: str = "user001"):
        self.store = store
        self.identity = identity

    # ---- recall ----
    def search(self, query: str, k: int = 5) -> list[dict]:
        return self.store.search_memories(query, k=k)

    def build_context_package(self, goal: str, task_id: str | None = None,
                              parent_id: str | None = None) -> dict:
        """按层级组装上下文包：task(续跑链优先) > project > experience > skill。

        每层受 BUDGET 字符预算约束（截内容不截条目数），总量恒定不随历史膨胀。
        """
        pkg = {
            "identity": {"user": self.identity},
            "task": {"id": task_id, "goal": goal[:400]},
            "project": [], "experience": [], "skill": [],
            "budget_chars": BUDGET,
        }
        # task 层：续跑链的父任务记忆整条带入（不靠关键词，确定性召回）
        if parent_id:
            for m in self.store.get_memories_by_task(parent_id):
                pkg["task"]["parent"] = {
                    "task_id": parent_id, "ok": m.get("content", "").startswith("[ok]"),
                    "memory": m.get("content", "")[:BUDGET["task"]],
                }
                break
        # 其余层级：关键词检索 + 预算截断
        hits = self.store.search_memories(goal, k=12)
        for tier in ("project", "experience", "skill"):
            left = BUDGET[tier]
            out = []
            for m in hits:
                if m.get("kind") != tier:
                    continue
                content = str(m.get("content", ""))[:left]
                if not content:
                    continue
                out.append({"kind": tier, "content": content, "importance": m.get("importance", 1)})
                left -= len(content)
                if left <= 50:
                    break
            pkg[tier] = out
        return pkg

    # ---- capture：任务完成自动入库（task 级，机器自动、零审核）----
    def on_task_result(self, task: dict):
        r = task.get("result") or {}
        tail = ((r.get("handoff") or {}).get("tail") or r.get("output") or "")[-400:]
        content = f"[{'ok' if r.get('ok') else 'failed'}] {task.get('goal', '')[:200]} → @{task.get('node_id')} {task.get('harness')} | 尾部: {tail}"
        self.store.add_memory(kind="task", scope="task", content=content,
                              importance=1, source_task=task["id"], confidence=0.5)

    # ---- 人工注入（即审即入：用户经飞书/REST 说的，本身就是审核）----
    def add_manual(self, kind: str, content: str, importance: int = 2) -> str:
        scope = "project" if kind == "project" else "user"
        return self.store.add_memory(kind=kind, scope=scope, content=content,
                                     importance=importance, confidence=0.8)

    # ---- capture / review（节点候选 → 审核，原有流程）----
    def ingest_candidate(self, payload: dict, source_task: str | None = None) -> str:
        return self.store.add_memory_candidate(payload, source_task)

    def list_candidates(self, status: str | None = None) -> list[dict]:
        return self.store.list_memory_candidates(status=status)

    def review(self, cid: str, action: str, **kw) -> dict | None:
        return self.store.review_memory_candidate(cid, action, **kw)
