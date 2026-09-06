"""一体化（V0.8）：经验→技能结晶 + 管理面板命令。"""
from __future__ import annotations

import os
import time

import httpx
import pytest

from fabric.central.memory import MemoryManager
from fabric.central.store import Store


@pytest.fixture(autouse=True)
def _no_real_model(monkeypatch):
    monkeypatch.setenv("AF_EMBED_DISABLE", "1")


@pytest.fixture()
def mem(tmp_path):
    return MemoryManager(Store(str(tmp_path / "m.db")))


def test_crystallize_needs_min_sources(mem):
    mem.add_manual("experience", "免费模型 504 时任务失败但文件已写")
    res = __import__("asyncio").run(mem.crystallize("免费模型故障处理"))
    assert not res["ok"] and "素材" not in res["hint"] or "条" in res["hint"]


def test_crystallize_creates_skill(mem):
    mem.add_manual("experience", "免费模型挂死排查：504超时时任务failed但文件已写好，直接resume续跑")
    mem.add_manual("experience", "nemotron免费模型会流式挂死，480s行超时兜底杀掉后handoff照常上报")
    mem.add_manual("project", "无关项目知识干扰项")
    res = __import__("asyncio").run(mem.crystallize("免费模型故障处理"))
    assert res["ok"] and res["n_sources"] == 2
    skill = [m for m in mem.store.all_memories() if m["kind"] == "skill"]
    assert len(skill) == 1
    assert "免费模型故障处理" in skill[0]["content"] and "[来源]" in skill[0]["content"]


def test_crystallize_score_floor_filters_noise(mem):
    # 低相关杂音（相同 bigram 少、向量分低）不应混入素材
    mem.add_manual("experience", "免费模型挂死时文件已写好，resume 可续跑")
    mem.add_manual("experience", "免费层上游504超时属常态，模型挂死可直接重试或换节点")
    mem.add_manual("experience", "飞书 open_id 按应用隔离完全无关的干扰内容另一主题")
    res = __import__("asyncio").run(mem.crystallize("免费模型挂死处理"))
    assert res["ok"] and res["n_sources"] == 2, res
    assert all("飞书" not in l for l in res["draft"].splitlines())


def test_crystallize_excludes_skill_kind(mem):
    mem.add_manual("experience", "经验A关于部署")
    mem.add_manual("experience", "经验B关于部署")
    __import__("asyncio").run(mem.crystallize("部署"))  # 生成 skill 1
    res = __import__("asyncio").run(mem.crystallize("部署"))  # 再次结晶：skill 不算素材
    assert res["ok"] and res["n_sources"] == 2


def test_panel_commands_e2e(cluster):
    port = cluster
    with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=120) as c:  # V0.10 crystallize 内联等蒸馏
        # 造两条经验 → 结晶 → 面板查看
        c.post("/api/say", json={"text": "memory add experience 免费模型挂死排查：504超时时文件已写好直接resume续跑"})
        c.post("/api/say", json={"text": "memory add experience 504上游超时属免费层常态，重试或换节点"})
        r = c.post("/api/say", json={"text": "memory crystallize 免费模型故障处理"}).json()["reply"]
        assert "已结晶" in r and "条经验" in r  # 蒸馏链路 E2E 只验通；质量真机验
        r = c.post("/api/say", json={"text": "skills"}).json()["reply"]
        assert "技能库" in r and "1 条" in r
        # tasks 面板
        r = c.post("/api/say", json={"text": "tasks 5"}).json()["reply"]
        assert "任务" in r
        # memories 面板
        r = c.post("/api/say", json={"text": "memories"}).json()["reply"]
        assert "记忆库" in r and "skill" in r
