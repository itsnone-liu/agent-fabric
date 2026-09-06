"""一体化审核闭环（V0.8）：candidates 面板 + review 命令。"""
from __future__ import annotations

import httpx


def test_review_flow_e2e(cluster):
    port = cluster
    with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=15) as c:
        # 造候选（run 一个 echo 任务会自动回流经验候选）
        tid = c.post("/api/say", json={"text": "run @node-a echo 审核闭环验证"}).json()["task_id"]
        import time
        for _ in range(50):
            t = [t for t in c.get("/api/tasks").json()["tasks"] if t["id"] == tid]
            if t and t[0]["status"] in ("done", "failed"):
                break
            time.sleep(0.2)
        r = c.post("/api/say", json={"text": "candidates"}).json()["reply"]
        assert "待审经验候选" in r and "review" in r
        # promote 第一条
        cid = r.splitlines()[1].split()[1]
        r = c.post("/api/say", json={"text": f"review {cid} promote"}).json()["reply"]
        assert "promote 完成 1 条" in r
        # 入库成 experience
        mems = c.get("/api/memory", params={"q": "审核闭环"}).json()["results"]
        assert any(m["kind"] == "experience" for m in mems)
        # 非法动作拒绝
        r = c.post("/api/say", json={"text": "review MC-notexist promote"}).json()["reply"]
        assert "完成 0 条" in r
