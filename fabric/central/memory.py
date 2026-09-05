"""中央记忆管理（V0 最小版）。

生命周期对齐 Mem0 的 triage→recall→dream 与 PLAN §18：
  节点 memory.candidate → pending（capture/triage）
  → 人工 review promote（成为 memory）或 discard
  → 检索组装 context_package（recall）
  → consolidate（dream）留待 V1（合并重复/冲突、置信度衰减）

V0 检索 = 关键词命中 + importance 加权（确定性，零 token）；
PG+pgvector 迁移后换 向量 + BM25 融合（对齐 dsh-evolve 已验证的混合检索）。
Scope 命名对齐 Agent Memory OS：private:user / agent:primary / project:xxx / global。
"""
from __future__ import annotations

from .store import Store

# Context Package token 预算（PLAN §20：恒定预算，不随历史膨胀）
BUDGET = {"identity": 200, "experience": 600, "project": 800, "task": 400}


class MemoryManager:
    def __init__(self, store: Store, identity: str = "user001"):
        self.store = store
        self.identity = identity

    # ---- recall ----
    def search(self, query: str, k: int = 5) -> list[dict]:
        return self.store.search_memories(query, k=k)

    def build_context_package(self, goal: str, task_id: str | None = None) -> dict:
        rel = self.search(goal, k=5)
        pkg = {
            "identity": {"user": self.identity},
            "task": {"id": task_id, "goal": goal[:400]},
            "experience": [
                {"kind": m["kind"], "content": m["content"][:300], "importance": m["importance"]}
                for m in rel
            ],
            "budget_tokens": BUDGET,
        }
        return pkg

    # ---- capture / review ----
    def ingest_candidate(self, payload: dict, source_task: str | None = None) -> str:
        return self.store.add_memory_candidate(payload, source_task)

    def list_candidates(self, status: str | None = None) -> list[dict]:
        return self.store.list_memory_candidates(status=status)

    def review(self, cid: str, action: str, **kw) -> dict | None:
        return self.store.review_memory_candidate(cid, action, **kw)
