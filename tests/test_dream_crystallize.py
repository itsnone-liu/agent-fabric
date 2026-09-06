"""V0.10 记忆质量三件套：dream 整理 / 蒸馏结晶 / memory forget。"""
from __future__ import annotations

import asyncio
import time

import numpy as np
import pytest

from fabric.central.memory import MemoryManager
from fabric.central.store import Store


@pytest.fixture()
def mem(tmp_path):
    s = Store(str(tmp_path / "t.db"))
    m = MemoryManager(s)

    class _NoEmbed:  # 测试走 BM25-only；向量手动塞进库（merge 用）
        disabled = True

        def embed(self, texts):
            return []

    m.embedder = _NoEmbed()
    return m


def _add(mem, kind, content, vec=None, importance=1, updated=None):
    mid = mem.store.add_memory(kind, "project", content, importance)
    if vec is not None:
        mem.store.set_memory_embedding(mid, np.asarray(vec, dtype=np.float32).tobytes())
    return mid


def test_dream_trims_task_flow(mem):
    for i in range(55):
        _add(mem, "task", f"[ok] 流水任务{i}", updated=time.time())
    stat = mem.dream(task_keep=50)
    rows = [m for m in mem.store.all_memories(1000) if m["kind"] == "task"]
    assert stat["task_trimmed"] == 5 and len(rows) == 50


def test_dream_merges_similar_experience(mem):
    v = [1.0] + [0.0] * 511
    a = _add(mem, "experience", "matplotlib中文乱码要设 rcParams font.family", vec=v, importance=2)
    b = _add(mem, "experience", "matplotlib 中文需配置 rcParams 字体（重复经验）", vec=v, importance=2)
    stat = mem.dream()
    ids = [m["id"] for m in mem.store.all_memories(100)]
    assert stat["merged"] == 1
    assert (a in ids) != (b in ids)  # 恰好留一条
    kept = next(m for m in mem.store.all_memories(100) if m["id"] in ids and m["kind"] == "experience")
    assert "合并自" in kept["content"]


def test_dream_keeps_dissimilar(mem):
    _add(mem, "experience", "部署三步走", vec=[1.0] + [0.0] * 511)
    _add(mem, "experience", "完全无关的教训", vec=[0.0, 1.0] + [0.0] * 510)
    assert mem.dream()["merged"] == 0


def test_crystallize_distill_via_fake_tm(mem):
    _add(mem, "experience", "matplotlib中文字体 rcParams font.family SimHei", vec=[1.0] + [0.0] * 511)
    _add(mem, "task", "[ok] matplotlib 画正弦波中文字体正常", vec=[1.0] + [0.0] * 511)

    class FakeTM:
        class _S(dict):
            pass

        def __init__(self):
            self.created = None

        def create(self, goal, harness, node_id=None, internal=False):
            self.created = goal
            return {"id": "T-distill", "goal": goal, "harness": harness, "node_id": node_id}

        async def dispatch(self, task):
            return "ok"

        class _Store:
            def get_task(self, tid):
                return {"id": tid, "status": "done",
                        "result": {"output": "> build · m\n【技能】matplotlib 中文显示\n1. rcParams 设置\n[LESSON] 无"}}

        store = _Store()

    tm = FakeTM()
    res = asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
        mem.crystallize("matplotlib 中文", tm=tm))
    assert res["ok"] and res["method"] == "蒸馏"
    assert "【技能】matplotlib 中文显示" in res["draft"] and "rcParams" in res["draft"]
    assert "素材" in tm.created and "技能文档" in tm.created


def test_crystallize_fallback_when_task_fails(mem):
    _add(mem, "experience", "经验甲 matplotlib 字体", vec=[1.0] + [0.0] * 511)
    _add(mem, "experience", "经验乙 matplotlib rcParams", vec=[1.0] + [0.0] * 511)

    class FakeTM:
        def create(self, goal, harness, node_id=None, internal=False):
            return {"id": "T-x", "goal": goal, "harness": harness, "node_id": node_id}

        async def dispatch(self, task):
            return "ok"

        class store:
            @staticmethod
            def get_task(tid):
                return {"id": tid, "status": "failed", "result": {"output": "boom"}}

    res = asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
        mem.crystallize("matplotlib 中文", tm=FakeTM()))
    assert res["ok"] and res["method"] == "拼接"
    assert "经验甲" in res["draft"]
