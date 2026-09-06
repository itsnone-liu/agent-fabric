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

_ANSI = None


def _clean(text: str) -> str:
    """去终端 ANSI 色码/控制符（task 输出尾部常带）。"""
    global _ANSI
    if _ANSI is None:
        import re
        _ANSI = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
    return _ANSI.sub("", text)


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
        # 融合：任一路径命中即可入围；单路径不可用时权重重归一化
        # （0.6/0.4 预设双路径；BM25-only 上限 0.4 会压死下游阈值，如 crystallize 的 score_floor）
        if qv is not None:
            vw, bw = VEC_WEIGHT, BM25_WEIGHT
        else:  # 向量不可用：全权重给 BM25
            vw, bw = 0.0, 1.0
        fused = vw * v_norm + bw * b_norm
        fused[b_norm + v_norm <= 0] = -1.0
        # 检索内归一化：_score = 相对本次最佳命中的相关度(0~1)。
        # 向量模式绝对分系统性偏低(0.6×cos)，跨模式绝对阈值不可通约，归一化后统一。
        fmax = float(fused[fused > 0].max()) if (fused > 0).any() else 0.0
        if fmax > 0:
            fused = np.where(fused > 0, fused / fmax, -1.0)
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
        try:  # 命中计数（dream 衰减依据）；失败不影响检索
            self.store.bump_memory_hits([m["id"] for m in out])
        except Exception:
            pass
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
        tail = _clean(((r.get("handoff") or {}).get("tail") or r.get("output") or ""))[-400:]
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

    # ---- dream：定期整理（V0.10 确定性 consolidation，零 LLM）----
    def dream(self, task_keep: int = 50, merge_cos: float = 0.93) -> dict:
        """去水三招：task 流水限额删除；同 kind 高相似合并（计数并入）；
        长期零命中且低权重的 task 记忆降权。返回统计。"""
        import time as _t
        rows = self.store.all_memories(limit=100000)
        now = _t.time()
        stat = {"task_trimmed": 0, "merged": 0, "demoted": 0}
        # ① task 流水只留最近 N 条
        tasks_sorted = sorted([m for m in rows if m["kind"] == "task"],
                              key=lambda m: m.get("updated_at") or 0)
        for m in tasks_sorted[:-task_keep] if len(tasks_sorted) > task_keep else []:
            self.store.delete_memory(m["id"])
            stat["task_trimmed"] += 1
        # ② 同 kind 高相似合并（experience/skill/project 才值得保真；task 已限额）
        by_kind: dict[str, list] = {}
        for m in rows:
            if m["kind"] in ("experience", "skill", "project") and m.get("embedding"):
                by_kind.setdefault(m["kind"], []).append(m)
        for kind, ms in by_kind.items():
            dead = set()
            for i in range(len(ms)):
                if ms[i]["id"] in dead:
                    continue
                a = np.frombuffer(ms[i]["embedding"], dtype=np.float32)
                for j in range(i + 1, len(ms)):
                    if ms[j]["id"] in dead:
                        continue
                    b = np.frombuffer(ms[j]["embedding"], dtype=np.float32)
                    if len(a) != len(b):
                        continue
                    if float(a @ b) >= merge_cos:  # 已归一化
                        keep, drop = ms[i], ms[j]
                        if (keep.get("updated_at") or 0) < (drop.get("updated_at") or 0):
                            keep, drop = drop, keep  # 留新的
                        merged = (f"{_clean(str(keep['content'])[:1500])}\n"
                                  f"[合并自 {drop['id']}·重复经验]")
                        with self.store._lock:  # 与在线写并发安全（dream 在后台线程）
                            self.store.db.execute("DELETE FROM memories WHERE id=?", (drop["id"],))
                            self.store.db.execute(
                                "UPDATE memories SET content=?, hits=COALESCE(hits,0)+?, updated_at=? WHERE id=?",
                                (merged, (drop.get("hits") or 0), now, keep["id"]))
                            self.store.db.commit()
                        dead.add(drop["id"])
                        stat["merged"] += 1
        # ③ 零命中老 task 降权（不删——可能仍是 resume 链锚点）
        with self.store._lock:
            for m in rows:
                if (m["kind"] == "task" and (m.get("importance") or 1) >= 2
                        and not m.get("hits") and now - (m.get("updated_at") or 0) > 7 * 86400):
                    self.store.db.execute("UPDATE memories SET importance=1 WHERE id=?", (m["id"],))
                    stat["demoted"] += 1
            self.store.db.commit()
        return stat

    # ---- auto_crystallize：promote 后自动判断是否值得蒸馏（V0.10.1）----
    _last_auto = 0.0

    async def auto_crystallize(self, seed: str, tm=None, node: str | None = None,
                               min_sources: int = 3, cooldown: float = 3600.0) -> dict:
        """新经验 promote 后的自动蒸馏判断（用户 2026-09-06：自动进行）。

        触发条件（全满足）：①冷却期外 ②素材 ≥3 条 ③无高相似已有 skill
        （cos≥0.85 判重——同主题技能已存在就不再结）。蒸馏走节点免费模型，
        central 零 LLM key。返回 {"triggered": bool, ...}。
        """
        import time as _t
        now = _t.time()
        if now - self._last_auto < cooldown:
            return {"triggered": False, "reason": "cooldown"}
        cand = [m for m in self.search(seed, k=10)
                if m.get("kind") in ("experience", "task") and m.get("_score", 0.0) >= 0.25]
        if len(cand) < min_sources:
            return {"triggered": False, "reason": f"素材不足({len(cand)}<{min_sources})"}
        # 判重：已有 skill 与种子语义足够近 → 跳过
        if self.embedder is not None and not getattr(self.embedder, "disabled", False):
            try:
                seed_vec = np.asarray(self.embedder.embed([seed])[0], dtype=np.float32)
                for m in self.store.all_memories(500):
                    if m.get("kind") != "skill" or not m.get("embedding"):
                        continue
                    v = np.frombuffer(m["embedding"], dtype=np.float32)
                    if len(v) == len(seed_vec) and float(seed_vec @ v) >= 0.85:
                        return {"triggered": False, "reason": f"已有同主题技能 {m['id']}"}
            except Exception:
                pass  # 判重失败不阻塞——宁可多结一次
        self._last_auto = now
        # seed 为空（review 未带内容等）时用 top 素材兜底命名/判重
        if not seed.strip():
            seed = str(cand[0].get("content") or "")[:200]
        topic = seed.strip()[:40] or "自动结晶"
        res = await self.crystallize(topic, tm=tm, node=node, min_sources=min_sources)
        out = {"triggered": bool(res.get("ok")), "topic": topic}
        out.update({k: res.get(k) for k in ("skill_id", "n_sources", "method", "draft", "hint")})
        return out

    # ---- crystallize：经验→技能结晶（V0.10：节点免费模型蒸馏，失败退拼接）----
    async def crystallize(self, query: str, tm=None, node: str | None = None,
                          min_sources: int = 2, k: int = 10) -> dict:
        """把检索聚类的相关经验/任务记忆结晶成 skill。

        V0.10：素材先经节点免费模型蒸馏（LLM 消耗可控——走 fabric 自己的任务
        管道，免费模型零成本），失败/超时退回确定性拼接草稿。
        规则：素材限 experience/task 两级（skill 不吃自己，避免滚雪球）；
        相关性绝对下限 0.25 剔杂音；experience 排前；不足 min_sources 条拒绝。
        """
        cand = [m for m in self.search(query, k=k)
                if m.get("kind") in ("experience", "task") and m.get("_score", 0.0) >= 0.25]
        if not cand:
            return {"ok": False, "n_sources": 0,
                    "hint": "无相关经验（experience/task）——先积累或换关键词"}
        hits = cand
        hits.sort(key=lambda m: (m.get("kind") != "experience", -m.get("_score", 0)))
        if len(hits) < min_sources:
            return {"ok": False, "n_sources": len(hits),
                    "hint": f"相关经验仅 {len(hits)} 条（需≥{min_sources}），先积累或换关键词"}
        srcs = [f"{m['id']}({m['kind']})" for m in hits]
        material = "\n".join(f"- {_clean(str(m.get('content', '')))[:180]}" for m in hits)

        distilled = None
        if tm is not None:
            try:
                distilled = await self._distill_via_node(query, material, tm, node)
            except Exception:
                distilled = None  # 蒸馏失败不影响——退回拼接
        if distilled:
            draft = distilled
            method = "蒸馏"
        else:
            draft = (f"【技能】{query.strip()[:60]}（{len(hits)} 条经验结晶）\n"
                     + material + f"\n[来源] {' '.join(srcs)}")
            method = "拼接"
        mid = self._insert("skill", "user", draft, 2, None, 0.6)
        return {"ok": True, "skill_id": mid, "n_sources": len(hits),
                "method": method, "draft": draft}

    async def _distill_via_node(self, query: str, material: str, tm, node: str | None,
                                timeout: float = 420.0) -> str | None:
        """派蒸馏任务到节点免费模型，轮询至终态，返回蒸馏后的技能正文。

        蒸馏任务本身走完整任务管道（handoff/LESSON/进度推送）——fabric 吃自己
        的狗粮；这就是"LLM 消耗可控"的落地：免费模型、单次、超时兜底。
        """
        import asyncio
        import time as _t
        goal = (f"把以下经验素材提炼成一条可复用的技能文档。要求：\n"
                f"1. 开头【技能】+ 一句话主题\n"
                f"2. 适用场景（什么时候用）\n"
                f"3. 操作步骤（编号，可执行，含关键命令/路径，合并重复项）\n"
                f"4. 注意事项（坑）\n"
                f"5. 最后单行 [来源] 保留素材 id\n"
                f"只输出技能文档本身，不要额外解释。\n\n"
                f"主题：{query.strip()[:80]}\n素材：\n{material}")
        task = tm.create(goal, "opencode", node, internal=True)  # 静默：不推送结果/进度
        await tm.dispatch(task)
        t0 = _t.time()
        first = True
        while _t.time() - t0 < timeout:
            await asyncio.sleep(1 if first else 3)
            first = False
            t = tm.store.get_task(task["id"])
            if t and t.get("status") in ("done", "failed", "canceled"):
                if t["status"] != "done":
                    return None
                from .reply_clean import extract_reply
                body = extract_reply(str((t.get("result") or {}).get("output") or ""))
                return body or None
        return None

    # ---- capture / review（节点候选 → 审核，原有流程）----
    def ingest_candidate(self, payload: dict, source_task: str | None = None) -> str:
        return self.store.add_memory_candidate(payload, source_task)

    def list_candidates(self, status: str | None = None) -> list[dict]:
        return self.store.list_memory_candidates(status=status)

    def review(self, cid: str, action: str, **kw) -> dict | None:
        return self.store.review_memory_candidate(cid, action, **kw)
