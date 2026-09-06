"""三级记忆（task/project/experience/skill）+ 检索预算 测试。"""
from __future__ import annotations

import time

import httpx
import pytest

from fabric.central.memory import BUDGET, MemoryManager
from fabric.central.store import Store
from fabric.node.adapters.opencode import _compose_prompt
from fabric.node.adapters.base import RunContext
from pathlib import Path


# ---------- 单元 ----------

@pytest.fixture()
def mem(tmp_path):
    return MemoryManager(Store(str(tmp_path / "m.db")))


def test_task_memory_auto_capture(mem):
    task = {"id": "T-auto1", "goal": "部署演示", "harness": "opencode", "node_id": "mapian",
            "status": "done", "result": {"ok": True, "output": "部署完成，版本v1"}}
    mem.on_task_result(task)
    rows = mem.store.get_memories_by_task("T-auto1")
    assert len(rows) == 1 and rows[0]["kind"] == "task"
    assert "[ok]" in rows[0]["content"] and "部署演示" in rows[0]["content"]


def test_manual_add_and_budget(mem):
    mem.add_manual("project", "所有脚本放 scripts/ 目录，输出用 utf-8")
    mem.add_manual("skill", "部署流程：rsync → systemctl restart → journalctl 验证")
    pkg = mem.build_context_package("脚本 scripts 部署 流程")
    assert any("scripts/" in m["content"] for m in pkg["project"])
    assert any("rsync" in m["content"] for m in pkg["skill"])
    # 预算：各层内容总长不超过层级预算
    for tier in ("project", "experience", "skill"):
        total = sum(len(m["content"]) for m in pkg[tier])
        assert total <= BUDGET[tier] + 50


def test_budget_truncates_long_memories(mem):
    mem.add_manual("project", "A" * 5000)
    pkg = mem.build_context_package("A")
    total = sum(len(m["content"]) for m in pkg["project"])
    assert total <= BUDGET["project"] + 50


def test_parent_memory_in_package(mem):
    task = {"id": "T-p1", "goal": "父任务目标", "harness": "opencode", "node_id": "mapian",
            "status": "done", "result": {"ok": True, "output": "父任务输出尾部"}}
    mem.on_task_result(task)
    pkg = mem.build_context_package("任意新目标", task_id="T-p2", parent_id="T-p1")
    assert pkg["task"]["parent"]["task_id"] == "T-p1"
    assert "父任务目标" in pkg["task"]["parent"]["memory"]


def test_compose_prompt_renders_tiers():
    ctx = RunContext(workspace=Path("/tmp"), context_package={
        "task": {"id": "T-9", "parent": {"task_id": "T-8", "memory": "[ok] 上轮做了X"}},
        "project": [{"kind": "project", "content": "脚本放scripts/", "importance": 2}],
        "experience": [{"kind": "experience", "content": "zen免费层偶发504", "importance": 3}],
        "skill": [{"kind": "skill", "content": "部署三步流程", "importance": 2}],
    })
    p = _compose_prompt("继续干活", ctx)
    assert "<context>" in p and "继续干活" in p
    assert "T-8" in p and "scripts/" in p and "504" in p and "部署三步" in p
    # 无记忆时退化为裸 goal
    assert _compose_prompt("裸目标", RunContext(workspace=Path("/tmp"))) == "裸目标"


# ---------- E2E（复用 handoff 的 cluster 模式，最小化）----------

def test_memory_add_via_say_and_inject(cluster, ):  # noqa: E999
    port = cluster
    with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=15) as c:
        r = c.post("/api/say", json={"text": "memory add project 测试约定TESTMARK：产出文件必须带 TESTMARK 前缀"})
        assert "已入库" in r.json()["reply"]
        # 非法 kind 拒绝
        r = c.post("/api/say", json={"text": "memory add bogus xxx"})
        assert "kind" in r.json()["reply"]

        # 任务完成后 task 级记忆自动入库
        tid = c.post("/api/say", json={"text": "run @node-a opencode TESTMARK 任务记忆验证"}).json()["task_id"]
        end = time.time() + 15
        while time.time() < end:
            cur = [t for t in c.get("/api/tasks").json()["tasks"] if t["id"] == tid]
            if cur and cur[0]["status"] in ("done", "failed"):
                break
            time.sleep(0.2)
        mems = c.get("/api/memory", params={"q": "TESTMARK"}).json()["results"]
        assert any(m["kind"] == "project" for m in mems)
        assert any(m["kind"] == "task" and tid in (m.get("source_task") or "") for m in mems)
