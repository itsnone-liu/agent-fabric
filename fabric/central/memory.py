"""中央记忆管理（V0.7：三级记忆 + BM25×向量混合检索）。

生命周期对齐 Mem0 的 triage→recall→dream 与 PLAN §18：
  节点 memory.candidate → pending（capture/triage）
  → 人工 review promote（成为 memory）或 discard
  → 检索组装 context_package（recall，混合检索 + 层级预算截断）
  → consolidate（dream）留待 V1

三级记忆（PLAN §12）：
  task    机器自动：每个任务终态即记（goal+尾部+节点+成败），resume 链优先召回
  project 人工注入：项目约定/决策（memory add project ...）
  skill   人工沉淀：可复用操作程序（memory add skill ...）
  experience 节点回流候选 promote（原有）

检索（V0.7）：BM25（store._tokenize 的 CJK bigram+拉丁词）× 本地向量
（fastembed BAAI/bge-small-zh-v1.5，512维，零API费用）0.6/0.4 加权融合，
语义改写不共享 bigram 也能召回；向量未就绪时自动退化为纯 BM25。
V1 迁 PG+pgvector+BM25 时接口同构。Scope 命名对齐 Agent Memory OS。
"""
from __future__ import annotations

import os
import threading

import numpy as np

from .store import Store, _tokenize

# Context Package 预算（PLAN §20：恒定预算，不随历史膨胀；V0 按≈4字符/token折算）
BUDGET = {"identity": 200, "task": 400, "project": 800, "experience": 600, "skill": 600}

MANUAL_KINDS = {"project", "skill", "fact", "lesson", "decision", "preference", "experience"}

VEC_WEIGHT, BM25_WEIGHT = 0.6, 0.4


def _bm25_scores(docs: list[str], query: str, k1: float = 1.5, b: float = 0.75) -> np.ndarray:
    """确定性 BM25（Lucene 式非负 IDF）。docs 为空返回空数组。"""
    doc_toks = [list(_tokenize(d)) for d in docs]
    n_docs = len(doc_toks)
    if n_docs == 0:
        return np.zeros(0, dtype=np.float32)
    avgdl = sum(len(t) for t in doc_toks) / n_docs
    df: dict[str, int] = {}
    for toks in doc_toks:
        for t in set(toks):
            df[t] = df.get(t, 0) + 1
    qtoks = _tokenize(query)
    scores = np.zeros(n_docs, dtype=np.float32)
    for i, toks in enumerate(doc_toks):
        tf: dict[str, int] = {}
        for t in toks:
            tf[t] = tf.get(t, 0) + 1
        dl = len(toks)
        norm = k1 * (1 - b + b * dl / avgdl) if avgdl > 0 else k1
        s = 0.0
        for t in qtoks:
            if t not in tf:
                continue
            idf = np.log(1 + (n_docs - df[t] + 0.5) / (df[t] + 0.5))
            s += idf * tf[t] * (k1 + 1) / (tf[t] + norm)
        scores[i] = s
    return scores


class Embedder:
    """fastembed 本地向量器：惰性加载（首次用可能下载模型~100MB，走 hf-mirror），
    就绪前所有 embed 返回 None（调用方退化为 BM25）。线程安全单例语义由 MemoryManager 持有。"""

    def __init__(self, model: str | None = None):
        self.model_name = model or os.getenv("AF_EMBED_MODEL", "BAAI/bge-small-zh-v1.5")
        self.disabled = os.getenv("AF_EMBED_DISABLE") == "1"
        self._model = None
        self._lock = threading.Lock()
        self._tried = False

    @property
    def ready(self) -> bool:
        return self._model is not None

    def _ensure(self):
        if self.disabled or self._tried:
            return
        with self._lock:
            if self._model is not None or self._tried:
                return
            self._tried = True
            try:
                os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
                from fastembed import TextEmbedding
                self._model = TextEmbedding(self.model_name)
            except Exception as e:  # 模型不可得：永久退化为 BM25（不影响主流程）
                print(f"[memory] 向量模型不可用，退化为纯BM25: {e!r}", flush=True)

    def embed(self, texts: list[str]) -> list[np.ndarray | None]:
        self._ensure()
        if not self._model:
            return [None] * len(texts)
        try:
            vecs = [np.asarray(v, dtype=np.float32) for v in self._model.embed(texts)]
            # 归一化，点积即余弦
            return [v / (np.linalg.norm(v) + 1e-9) for v in vecs]
        except Exception:
            return [None] * len(texts)


class MemoryManager:
    def __init__(self, store: Store, identity: str = "user001", embedder: Embedder | None = None):
        self.store = store
        self.identity = identity
        self.embedder = embedder if embedder is not None else Embedder()
        self._backfill_once = False
        self._kick_backfill()

    # ---- 向量回填（后台、一次性、失败静默）----
    def _kick_backfill(self):
        if self.embedder.disabled or self._backfill_once:
            return
        self._backfill_once = True
        threading.Thread(target=self._backfill, daemon=True, name="embed-backfill").start()

    def _backfill(self):
        try:
            rows = [m for m in self.store.all_memories() if not m.get("embedding")]
            if not rows:
                return
            vecs = self.embedder.embed([m["content"] for m in rows])
            for m, v in zip(rows, vecs):
                if v is not None:
                    self.store.set_memory_embedding(m["id"], v.tobytes())
            print(f"[memory] 向量回填完成 {sum(1 for v in vecs if v is not None)}/{len(rows)}", flush=True)
        except Exception as e:
            print(f"[memory] 向量回填失败（忽略）: {e!r}", flush=True)

    # ---- recall：BM25 × 向量 融合 ----
    def search(self, query: str, k: int = 5) -> list[dict]:
        rows = self.store.all_memories()
        if not rows:
            return []
        # BM25 分（手写 Lucene 式：idf=ln(1+(N-n+.5)/(n+.5)) 恒非负，
        # 微语料/单文档也稳健；rank_bm25 库的 epsilon 地板会在小语料上把命中打成负分）
        try:
            b_scores = _bm25_scores([m["content"] or "" for m in rows], query)
            b_max = float(b_scores.max()) if b_scores.size else 0.0
            b_norm = b_scores / b_max if b_max > 0 else b_scores
        except Exception:
            b_norm = np.zeros(len(rows), dtype=np.float32)
        # 向量分（缺向量的行记 0；query 向量失败则全 0）
        qv = self.embedder.embed([query])[0] if not self.embedder.disabled else None
        v_norm = np.zeros(len(rows), dtype=np.float32)
        if qv is not None:
            mat = []
            for m in rows:
                e = m.get("embedding")
                if e:
                    arr = np.frombuffer(e, dtype=np.float32)
                    if len(arr) == len(qv):
                        mat.append(arr)
                    else:
                        mat.append(np.zeros(len(qv), dtype=np.float32))
                else:
                    mat.append(np.zeros(len(qv), dtype=np.float32))
            v_norm = np.asarray(mat, dtype=np.float32) @ qv  # 已归一化，点积=余弦
            v_norm = np.clip(v_norm, 0.0, 1.0)
        # 融合：任一路径命中即可入围
        fused = VEC_WEIGHT * v_norm + BM25_WEIGHT * b_norm
        fused[b_norm + v_norm <= 0] = -1.0
        order = np.argsort(-fused)[:k]
        out = []
        for i in order:
            if fused[i] <= 0:
                break
            m = dict(rows[i])
            m.pop("embedding", None)
            m["_score"] = round(float(fused[i]), 4)
            m.pop("keywords", None)
            out.append(m)
        return out

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
        # task 层：续跑链的父任务记忆整条带入（不靠检索，确定性召回）
        if parent_id:
            for m in self.store.get_memories_by_task(parent_id):
                pkg["task"]["parent"] = {
                    "task_id": parent_id, "ok": m.get("content", "").startswith("[ok]"),
                    "memory": m.get("content", "")[:BUDGET["task"]],
                }
                break
        # 其余层级：混合检索 + 预算截断
        hits = self.search(goal, k=12)
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
        self._insert("task", "task", content, 1, task["id"], 0.5)

    # ---- 人工注入（即审即入：用户经飞书/REST 说的，本身就是审核）----
    def add_manual(self, kind: str, content: str, importance: int = 2) -> str:
        scope = "project" if kind == "project" else "user"
        return self._insert(kind, scope, content, importance, None, 0.8)

    def _insert(self, kind, scope, content, importance, source_task, confidence) -> str:
        vec = None if self.embedder.disabled else self.embedder.embed([content])[0]
        return self.store.add_memory(kind=kind, scope=scope, content=content,
                                     importance=importance, source_task=source_task,
                                     confidence=confidence,
                                     embedding=(vec.tobytes() if vec is not None else None))

    # ---- capture / review（节点候选 → 审核，原有流程）----
    def ingest_candidate(self, payload: dict, source_task: str | None = None) -> str:
        return self.store.add_memory_candidate(payload, source_task)

    def list_candidates(self, status: str | None = None) -> list[dict]:
        return self.store.list_memory_candidates(status=status)

    def review(self, cid: str, action: str, **kw) -> dict | None:
        return self.store.review_memory_candidate(cid, action, **kw)
