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


def _add(mem, kind, content, vec=None, importance=1, updated=None, source_task=None):
    mid = mem.store.add_memory(kind, "project", content, importance, source_task=source_task)
    if vec is not None:
        mem.store.set_memory_embedding(mid, np.asarray(vec, dtype=np.float32).tobytes())
    return mid


def test_dream_trims_task_flow(mem):
    """V1.1 State/Memory 分离：task 流水 dream 时一次性清空（State 留 tasks 表）。"""
    for i in range(55):
        _add(mem, "task", f"[ok] 流水任务{i}", updated=time.time())
    stat = mem.dream(task_keep=50)
    rows = [m for m in mem.store.all_memories(1000) if m["kind"] == "task"]
    assert stat["task_trimmed"] == 55 and len(rows) == 0


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


def test_auto_crystallize_cooldown_and_dedup(mem):
    import asyncio as _a
    v = (np.ones(512, dtype=np.float32) / np.sqrt(512)).tolist()  # 与 _SameVec 查询同向
    _add(mem, "experience", "matplotlib 中文字体 rcParams 配置", vec=v)
    _add(mem, "experience", "matplotlib 中文字体 rcParams SimHei 方案", vec=v)
    _add(mem, "experience", "matplotlib 中文字体 rcParams 负号 unicode_minus", vec=v)
    # 素材不足（<3）不触发（BM25-only 路径）
    m2 = MemoryManager(mem.store)
    m2.embedder = mem.embedder
    r = _a.run(m2.auto_crystallize("完全无关的主题 xyzzy", tm=None))
    assert not r["triggered"] and "素材不足" in r["reason"]
    # 已有同主题 skill（cos≥0.85）→ 跳过（常向量 embedder：任何查询与库内 cos=1）
    class _SameVec:
        disabled = False

        def embed(self, texts):
            return [np.ones(512, dtype=np.float32) / np.sqrt(512) for _ in texts]

    _add(mem, "skill", "【技能】matplotlib 中文字体 rcParams 配置与负号处理",
        vec=(np.ones(512, dtype=np.float32) / np.sqrt(512)).tolist())
    for t in ("T-a", "T-b"):  # V1.1：判重测试需素材跨 ≥2 任务才能走到判重
        _add(mem, "experience", "matplotlib 绘图经验", source_task=t,
             vec=(np.ones(512, dtype=np.float32) / np.sqrt(512)).tolist())
    m3 = MemoryManager(mem.store)
    m3.embedder = _SameVec()
    r = _a.run(m3.auto_crystallize("matplotlib 中文字体配置", tm=None))
    assert not r["triggered"] and "同主题" in r["reason"]


def test_auto_crystallize_requires_cross_task():
    """V1.1：素材≥3 且须来自≥2个不同任务（单任务经验不结晶）。"""
    import tempfile, os
    from fabric.central.memory import MemoryManager
    from fabric.central.store import Store
    s = Store(os.path.join(tempfile.mkdtemp(), "ct.db"))
    m = MemoryManager(s)

    class _SameVec:
        disabled = False
        def embed(self, t):
            import numpy as np
            texts = t if isinstance(t, list) else [t]
            return [np.ones(512, dtype=np.float32) / np.sqrt(512) for _ in texts]
    m.embedder = _SameVec()

    for i in range(3):  # 3 条经验全来自同一任务
        m.store.add_memory("experience", "project", f"经验{i} pip freeze 导出清单",
                           2, source_task="T-same", embedding=None)
    class _FakeTM:  # 不该走到蒸馏
        async def create(self, *a, **k): raise AssertionError("不应触发结晶")
        async def dispatch(self, t): raise AssertionError("不应触发结晶")
    import asyncio as _aio
    r = _aio.run(m.auto_crystallize("pip freeze 导出", _FakeTM()))
    assert not r["triggered"] and "跨任务" in r["reason"], r

    m.store.add_memory("experience", "project", "经验4 pip freeze 导出与版本核对",
                       2, source_task="T-other", embedding=None)
    import numpy as np
    m.store.add_memory("skill", "project", "【技能】pip freeze 导出",
                       2, source_task=None,
                       embedding=(np.ones(512, dtype=np.float32) / np.sqrt(512)).tobytes())
    r2 = _aio.run(m.auto_crystallize("pip freeze 导出", _FakeTM()))
    # 跨任务关已过（否则 reason 还是"未跨任务"）→ 现在应由判重挡下（同向量 cos=1）
    assert r2.get("reason") != "素材未跨任务验证(1个来源任务)", r2
