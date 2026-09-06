"""会话化指挥（V0.9）：use 选主机 → 自然语言直通 → 排队意见自动续跑。"""
from __future__ import annotations

import time

import httpx


def _wait_done(c, tid, timeout=20):
    end = time.time() + timeout
    while time.time() < end:
        t = [t for t in c.get("/api/tasks").json()["tasks"] if t["id"] == tid]
        if t and t[0]["status"] in ("done", "failed"):
            return t[0]
        time.sleep(0.2)
    return None


def test_session_flow_e2e(cluster):
    port = cluster
    with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=15) as c:
        # 未选主机 → 引导
        r = c.post("/api/say", json={"text": "帮我看看这个项目"}).json()["reply"]
        assert "use" in r
        # 选主机（别名）
        r = c.post("/api/say", json={"text": "use node-a"}).json()["reply"]
        assert "node-a" in r or "mapian" in r
        # 自然语言直通 → 派发 opencode（cluster 注册的 MakeFileAdapter）
        r = c.post("/api/say", json={"text": "创建 demo9.txt 写入 hello-session"}).json()
        tid = r["task_id"]
        assert tid and "opencode" in r["reply"]
        t = _wait_done(c, tid)
        assert t and t["status"] == "done"
        # 裸 @节点 切换
        r = c.post("/api/say", json={"text": "@node-b"}).json()["reply"]
        assert "node-b" in r
        # status 带会话快照
        r = c.post("/api/say", json={"text": "status"}).json()["reply"]
        assert "焦点节点" in r


def test_followup_auto_resume_e2e(cluster):
    port = cluster
    with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=15) as c:
        c.post("/api/say", json={"text": "use node-b"})
        # node-b 的 readfile adapter 带 2s 延迟：制造真实 running 窗口
        tid = c.post("/api/say", json={"text": "读一下 demo.txt"}).json()["task_id"]
        r = c.post("/api/say", json={"text": "顺便把内容也翻译成英文"}).json()["reply"]
        assert "已排队" in r and "自动续跑" in r, r
        _wait_done(c, tid)
        # 等自动续跑产生新任务（resume 链）
        end = time.time() + 15
        resumed = None
        while time.time() < end:
            tasks = c.get("/api/tasks").json()["tasks"]
            resumed = [t for t in tasks if t["id"] != tid
                       and (t.get("goal") or "").startswith("继续任务")]
            if resumed:
                break
            time.sleep(0.3)
        assert resumed, "自动续跑未触发"
        t = _wait_done(c, resumed[0]["id"])
        assert t and t["status"] == "done", t
