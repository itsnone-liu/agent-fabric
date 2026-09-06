"""LESSON 回流机制（V0.9.2）：模型自标经验，无则不回流。"""
from __future__ import annotations

import re

from fabric.central.reply_clean import extract_reply


def _lesson_from(output: str) -> str | None:
    """复刻 daemon._emit_memory_candidate 的解析逻辑做单测。"""
    m = re.findall(r"\[LESSON\]\s*(.+)", output or "")
    lesson = (m[-1] if m else "").strip()
    if not lesson or lesson.lower() in ("无", "none", "n/a", "-"):
        return None
    return lesson


def test_lesson_extracted_from_tail():
    out = "> build · m\n$ python3 x.py\nOK\n完成了。\n[LESSON] matplotlib中文字体要配置 rcParams 否则乱码"
    assert _lesson_from(out) == "matplotlib中文字体要配置 rcParams 否则乱码"


def test_lesson_none_suppressed():
    assert _lesson_from("干完了\n[LESSON] 无") is None
    assert _lesson_from("干完了，没有 LESSON 标记") is None


def test_lesson_last_wins():
    out = "[LESSON] 第一条被覆盖\n[LESSON] 最终采用 venv 内 python"
    assert "venv" in _lesson_from(out)


def test_lesson_stripped_from_user_reply():
    raw = "$ ls\na.txt\n全部完成。\n[LESSON] 麦片部署须同步 site-packages"
    r = extract_reply(raw)
    assert r and "全部完成" in r
    assert "[LESSON]" not in r and "site-packages" not in r


def test_auto_promote_neutral_vs_warn(mem_like=None):
    """V0.11：成功任务+非教训类自动入库；教训型留人审（真库直测）。"""
    import tempfile, os
    from fabric.central.memory import MemoryManager
    from fabric.central.store import Store
    s = Store(os.path.join(tempfile.mkdtemp(), "ap.db"))
    m = MemoryManager(s)

    class _NoEmbed:
        disabled = True
        def embed(self, t):
            return []
    m.embedder = _NoEmbed()

    r1 = m.ingest_candidate({"kind": "experience",
                             "content": "[opencode] pandas 中文列名直接 sort_values 可用（T-1 ok @mapian）"})
    assert r1["auto_promoted"] and r1["memory"].startswith("M-")
    pend = m.list_candidates(status="pending")
    assert not pend  # 中性经验不占审队列

    r2 = m.ingest_candidate({"kind": "experience",
                             "content": "[opencode] 504超时挂死别重试，直接 resume（T-2 failed @mapian）"})
    assert not r2["auto_promoted"]
    assert len(m.list_candidates(status="pending")) == 1  # 教训型留审


def test_edit_memory_rewrites_content():
    import tempfile, os, numpy as np
    from fabric.central.memory import MemoryManager
    from fabric.central.store import Store
    s = Store(os.path.join(tempfile.mkdtemp(), "ed.db"))
    m = MemoryManager(s)

    class _ConstVec:
        disabled = False
        def embed(self, t):
            return [np.ones(512, dtype=np.float32) / np.sqrt(512)]
    m.embedder = _ConstVec()

    mid = m.store.add_memory("experience", "user", "旧内容 matplotlib 字体", 2)
    m.store.set_memory_embedding(mid, (np.ones(512) / np.sqrt(512)).astype(np.float32).tobytes())
    r = m.edit_memory(mid, "更正后：rcParams font.family=SimHei 且 axes.unicode_minus=False")
    assert r["ok"]
    rows = [x for x in m.store.all_memories(10) if x["id"] == mid]
    assert rows and "更正后" in rows[0]["content"]
    assert rows[0]["embedding"], "编辑后向量应重算"
    assert m.edit_memory("M-nope", "x")["ok"] is False


def test_model_routing_to_payload():
    """V0.12.2：会话 model → task.model → TASK_START payload → adapter ctx。"""
    from fabric.central.tasks import TaskManager
    from fabric.central.store import Store
    from fabric.central.registry import NodeRegistry
    import tempfile, os, asyncio

    class _FakeWS:
        async def send_json(self, env):
            sent.append(env)
    sent = []
    st = Store(os.path.join(tempfile.mkdtemp(), "m.db"))
    reg = NodeRegistry()
    tm = TaskManager(st, reg, hub=None)
    ns = reg.register("node-a", {"harnesses": ["codex"]}, _FakeWS())
    task = tm.create("probe", "codex", "node-a", model="gpt-5.6-luna")
    asyncio.run(tm.dispatch(task))
    assert sent and sent[0]["payload"].get("model") == "gpt-5.6-luna"
