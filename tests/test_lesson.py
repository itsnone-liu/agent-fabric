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


def test_task_run_lifecycle_and_retry():
    """V1.2：dispatch 建 run；终态落 run 行；retry 同 task 新 run（换 harness）。"""
    from fabric.central.tasks import TaskManager
    from fabric.central.store import Store
    from fabric.central.registry import NodeRegistry
    import tempfile, os, asyncio

    sent = []
    class _WS:
        async def send_json(self, env): sent.append(env)
    st = Store(os.path.join(tempfile.mkdtemp(), "r.db"))
    reg = NodeRegistry()
    tm = TaskManager(st, reg, hub=None)
    reg.register("node-a", {"harnesses": ["opencode", "codex"]}, _WS())

    task = tm.create("probe", "opencode", "node-a", internal=True)  # internal：无推送依赖
    asyncio.run(tm.dispatch(task))
    assert sent and sent[-1]["payload"].get("run_id", "").startswith("R-")
    rid = sent[-1]["payload"]["run_id"]
    runs = st.list_runs(task["id"])
    assert len(runs) == 1 and runs[0]["status"] == "running"

    # 模拟 RESULT（done）→ on_event 走完整路径（task 派生 + run 落终态）
    from fabric.central import tasks as _tk
    asyncio.run(tm.on_event({"type": _tk.P.T_TASK_RESULT, "task_id": task["id"],
                             "payload": {"ok": True, "output": "完成", "run_id": rid},
                             "node_id": "node-a"}))
    r = st.get_run(rid)
    assert r and r["status"] in ("done", "failed"), r

    # retry 换 harness → 新 run
    t2, msg = asyncio.run(tm.retry(task["id"], harness="codex"))
    assert t2 and "R-" in sent[-1]["payload"].get("run_id", "")
    assert sent[-1]["payload"]["run_id"] != rid
    assert len(st.list_runs(task["id"])) == 2


def test_send_message_and_cancel_reason():
    """V2a：运行中插话走 T_TASK_MESSAGE；cancel 带原因存 task、resume 注入。"""
    from fabric.central.tasks import TaskManager, P
    from fabric.central.store import Store
    from fabric.central.registry import NodeRegistry
    import tempfile, os, asyncio

    sent = []
    class _WS:
        async def send_json(self, env): sent.append(env)
    st = Store(os.path.join(tempfile.mkdtemp(), "cm.db"))
    reg = NodeRegistry()
    tm = TaskManager(st, reg, hub=None)
    reg.register("node-a", {"harnesses": ["opencode"]}, _WS())

    task = tm.create("长任务", "opencode", "node-a", internal=True)
    asyncio.run(tm.dispatch(task))
    r = asyncio.run(tm.send_message(task["id"], "方向错了，改用方案B"))
    assert r == "已插话"
    assert sent[-1]["type"] == P.T_TASK_MESSAGE and "方案B" in sent[-1]["payload"]["text"]

    r2 = asyncio.run(tm.cancel(task["id"], reason="方案A有风险"))
    assert "原因已记录" in r2
    back = st.get_task(task["id"])
    assert back.get("cancel_reason") == "方案A有风险"


def test_handoff_parse_and_inject():
    """V2b：[HANDOFF] 自报块解析；retry 注入上一 run 结构化交接。"""
    from fabric.central.tasks import TaskManager, parse_handoff, P
    from fabric.central.store import Store
    from fabric.central.registry import NodeRegistry
    import tempfile, os, asyncio

    out = ("实现了基础功能。\n[HANDOFF]\n完成: 协议层\n决定: 用 stdio 不用 http\n待办: 重连测试\n[LESSON] 无")
    h = parse_handoff(out)
    assert h["completed"] == ["协议层"] and h["pending"] == ["重连测试"]

    sent = []
    class _WS:
        async def send_json(self, env): sent.append(env)
    st = Store(os.path.join(tempfile.mkdtemp(), "h.db"))
    reg = NodeRegistry()
    tm = TaskManager(st, reg, hub=None)
    reg.register("node-a", {"harnesses": ["opencode", "codex"]}, _WS())

    task = tm.create("适配器开发", "opencode", "node-a", internal=True)
    asyncio.run(tm.dispatch(task))
    rid = sent[-1]["payload"]["run_id"]
    asyncio.run(tm.on_event({"type": P.T_TASK_RESULT, "task_id": task["id"],
                             "payload": {"ok": True, "output": out, "run_id": rid},
                             "node_id": "node-a"}))
    back = st.get_task(task["id"])
    assert (back["result"] or {}).get("semantic", {})["pending"] == ["重连测试"]

    # retry 换 codex → 新 run 的 goal 带交接块
    t2, _ = asyncio.run(tm.retry(task["id"], harness="codex"))
    assert "上一 run 交接" in t2["goal"] and "重连测试" in t2["goal"]
    assert sent[-1]["payload"]["harness"] == "codex"


def test_node_serial_execution():
    """V2b §16：节点并发=1——连发两任务排队串行，执行窗口不重叠。"""
    import asyncio, time, tempfile, json
    from pathlib import Path
    from fabric.node.daemon import FabricNode
    from fabric.node.adapters.base import HarnessAdapter, RunContext
    from fabric.central import tasks as tk

    events = []
    from fabric.node.adapters.base import AdapterResult
    class SlowAdapter(HarnessAdapter):
        name = "slow"
        async def probe(self): return True
        async def start(self, goal, ctx, progress):
            events.append(("start", goal, time.monotonic()))
            await asyncio.sleep(0.3)
            events.append(("end", goal, time.monotonic()))
            return AdapterResult(ok=True, output=f"done {goal}")
        async def cancel(self): pass

    class _WS:
        def __init__(self): self.sent = []
        async def send_json(self, env): self.sent.append(env)
        async def send(self, raw): self.sent.append(json.loads(raw))

    node = FabricNode("n1", "t", "ws://x", adapters={"slow": SlowAdapter()},
                      workspace=str(Path(tempfile.mkdtemp())))
    ws = _WS()
    for i in (1, 2):
        node._task_q.put_nowait({"task_id": f"T-{i}", "node_id": "n1",
                                 "payload": {"task_id": f"T-{i}", "run_id": f"R-{i}",
                                             "goal": f"任务{i}", "harness": "slow",
                                             "context_package": {}}})
    async def _drive():
        w = asyncio.create_task(node._task_worker(ws))
        await asyncio.sleep(0.9)      # 两个 0.3s 任务串行 ≈0.6s
        await node._task_q.put(None)  # 哨兵退出
        await w
    asyncio.run(_drive())
    assert len(events) == 4
    # 任务2 start ≥ 任务1 end（串行，不重叠）
    t1_end = [e[2] for e in events if e[0] == "end" and e[1] == "任务1"][0]
    t2_start = [e[2] for e in events if e[0] == "start" and e[1] == "任务2"][0]
    assert t2_start >= t1_end, "并发了！"
    results = [e for e in ws.sent if e["type"] == tk.P.T_TASK_RESULT]
    assert len(results) == 2 and all(e["payload"].get("ok") for e in results)
    # run_id 随执行透传回 RESULT（V1.2）
    assert {e["payload"].get("run_id") for e in results} == {"R-1", "R-2"}
