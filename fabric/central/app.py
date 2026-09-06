"""中央控制器入口：WS 节点网关 + 调试/Channel REST + 飞书(可选)。

边界（PLAN §2/§3）：Channel 与 Memory 属于中央；节点不知道飞书存在。
"""
from __future__ import annotations

import asyncio
import hmac
import os

from fastapi import FastAPI, WebSocket, WebSocketDisconnect

from ..shared import config as cfg
from ..shared import protocol as P
from .channels import ChannelHub, ConsoleChannel
from .memory import MemoryManager
from .registry import NodeRegistry
from .router import Action, help_text, parse_command
from .store import Store
from .tasks import TERMINAL, TaskManager


def create_app(db_path: str | None = None) -> FastAPI:
    store = Store(db_path or cfg.db_path())
    registry = NodeRegistry()
    hub = ChannelHub([ConsoleChannel()])
    memory = MemoryManager(store)
    tm = TaskManager(store, registry, hub, memory=memory)
    from .session import SessionManager
    session = SessionManager(online_nodes=lambda: [n["node_id"] for n in registry.snapshot()
                                                   if n.get("status") == "online"])
    tm.session = session  # RESULT 后自动续跑排队意见（V0.9 会话化）

    app = FastAPI(title="Agent Fabric Central", version="0.1.0")
    app.state.store, app.state.registry, app.state.tm, app.state.memory = store, registry, tm, memory

    # ---------- REST ----------

    @app.get("/api/health")
    async def health():
        return {"ok": True, "protocol": P.PROTOCOL}

    @app.get("/api/nodes")
    async def nodes():
        return {"nodes": registry.snapshot()}

    @app.get("/api/tasks")
    async def tasks(limit: int = 50):
        return {"tasks": store.list_tasks(limit)}

    @app.get("/api/tasks/{task_id}")
    async def task_one(task_id: str):
        t = store.get_task(task_id)
        return t or {"error": "not found"}

    @app.get("/api/events")
    async def events(limit: int = 200, task_id: str | None = None):
        return {"events": store.list_events(limit, task_id)}

    @app.get("/api/memory")
    async def memory_list(q: str | None = None, k: int = 5):
        if q:
            return {"query": q, "results": memory.search(q, k)}
        return {"memories": store.list_memories()}

    @app.get("/api/memory/candidates")
    async def memory_candidates(status: str | None = "pending"):
        return {"candidates": memory.list_candidates(status=status)}

    @app.post("/api/memory/review")
    async def memory_review(body: dict):
        r = memory.review(body.get("id", ""), body.get("action", ""),
                          kind=body.get("kind"), scope=body.get("scope", "user"),
                          importance=int(body.get("importance", 1)))
        return r or {"error": "candidate not found"}

    @app.post("/api/say")
    async def say(body: dict):
        reply, task_id = await handle_user_text((body.get("text") or "").strip())
        out = {"reply": reply}
        if task_id:
            out["task_id"] = task_id
        return out

    # ---------- 用户入口统一处理（REST / 飞书共用）----------

    async def handle_user_text(text: str) -> tuple[str, str | None]:
        act: Action = parse_command(text)
        if act.kind == "status":
            return _fmt_status() + "\n" + session.snapshot(), None
        if act.kind == "use":
            if not act.node:
                return "用法：use <麦片|米线|节点id>（或裸 @节点）\n" + session.snapshot(), None
            return session.switch(act.node), None
        if act.kind == "unknown":
            # 会话式自然语言（V0.9）：选完主机后直接说话即任务
            if not session.focus:
                return ("👋 先选主机：use 麦片 / use 米线（或 @mapian）。"
                        "命令玩法发 help"), None
            last = tm.store.list_tasks(1)
            if last and last[0]["status"] not in ("done", "failed", "canceled"):  # 非终态=忙
                session.followups.append(act.raw)
                return (f"📥 已排队（第 {len(session.followups)} 条追加意见），"
                        f"{last[0]['id']} 完成后自动续跑"), None
            task = tm.create(act.raw, "opencode", session.focus)
            msg = await tm.dispatch(task)
            session.last_task = task["id"]
            return msg, task["id"]
        if act.kind == "cancel":
            return await tm.cancel(act.task_id or ""), None
        if act.kind == "resume":
            task, msg = await tm.resume(act.task_id or "", node_id=act.node,
                                        harness=None if act.harness == "echo" else act.harness,
                                        extra_goal=act.goal)
            return msg, (task["id"] if task else None)
        if act.kind == "run":
            task = tm.create(act.goal, act.harness, act.node)
            msg = await tm.dispatch(task)
            return msg, task["id"]
        if act.kind == "memory_list":
            cands = memory.list_candidates(status="pending")
            lines = [f"- {c['id']} [{c['kind']}] {c['content'][:60]}" for c in cands[:10]]
            return "待审记忆候选：\n" + ("\n".join(lines) if lines else "（空）"), None
        if act.kind == "memory_search":
            res = memory.search(act.goal, k=5)
            lines = [f"- [{m['kind']}] {m['content'][:60]}" for m in res]
            return "检索结果：\n" + ("\n".join(lines) if lines else "（无命中）"), None
        if act.kind == "memory_add":
            from .memory import MANUAL_KINDS
            kind, content = (act.harness or "").lower(), act.goal.strip()
            if kind not in MANUAL_KINDS:
                return f"kind 必须是 {'/'.join(sorted(MANUAL_KINDS))}，例：memory add project 工作区约定…", None
            if not content:
                return "内容为空，例：memory add project 所有脚本放 scripts/ 目录", None
            mid = memory.add_manual(kind, content)
            return f"✅ 已入库 [{kind}] {mid}: {content[:80]}", None
        if act.kind == "memory_crystallize":
            res = memory.crystallize(act.goal)
            if not res.get("ok"):
                return "🧊 " + res.get("hint", "素材不足"), None
            return (f"🧊 已结晶技能 {res['skill_id']}（{res['n_sources']} 条经验）\n"
                    + res["draft"][:600]), None
        if act.kind == "tasks_list":
            try:
                n = max(1, min(int(act.harness or 10), 20))
            except ValueError:
                n = 10
            rows = tm.store.list_tasks(n)
            lines = [f"- {t['id']} {t['status']:<7} @{t.get('node_id')} {t.get('harness')} | {(t.get('goal') or '')[:38]}"
                     for t in rows]
            return f"最近 {len(rows)} 个任务：\n" + ("\n".join(lines) if lines else "（无）"), None
        if act.kind == "task_detail":
            t = tm.store.get_task(act.task_id)
            if not t:
                return f"任务 {act.task_id} 不存在", None
            r = t.get("result") or {}
            ho = r.get("handoff") or {}
            lines = [f"{t['id']} {t['status']} @{t.get('node_id')} {t.get('harness')}",
                     f"目标: {(t.get('goal') or '')[:120]}",
                     f"尾部: {(r.get('output') or r.get('error') or '')[-160:]}"]
            if ho.get("files"):
                lines.append("工作区: " + ", ".join(list(ho["files"])[:8]))
            return "\n".join(lines), None
        if act.kind == "skills_list":
            rows = [m for m in memory.store.all_memories() if m.get("kind") == "skill"]
            lines = [f"- {m['id']} {str(m.get('content',''))[:70]}" for m in rows[:10]]
            return f"技能库 {len(rows)} 条：\n" + ("\n".join(lines) if lines else "（空，用 memory crystallize <主题> 结晶）"), None
        if act.kind == "memories_list":
            try:
                n = max(1, min(int(act.harness or 12), 30))
            except ValueError:
                n = 12
            rows = memory.store.all_memories()[:n]
            from collections import Counter
            stat = Counter(m.get("kind") for m in memory.store.all_memories())
            lines = [f"- [{m.get('kind')}] {str(m.get('content',''))[:60]}" for m in rows]
            return (f"记忆库 {dict(stat)}，最近 {len(rows)} 条：\n"
                    + ("\n".join(lines) if lines else "（空）")), None
        if act.kind == "candidates_list":
            rows = memory.list_candidates(status="pending")
            lines = [f"- {m['id']} [{m.get('kind','')}] {str(m.get('content',''))[:60]}" for m in rows[:15]]
            return (f"待审经验候选 {len(rows)} 条（review <id|all> <promote|discard>）：\n"
                    + ("\n".join(lines) if lines else "（空）")), None
        if act.kind == "review":
            action = act.harness or ""
            if action not in ("promote", "discard"):
                return "用法：review <候选id|all> <promote|discard>", None
            ids = ([m["id"] for m in memory.list_candidates(status="pending")]
                   if act.task_id == "all" else [act.task_id])
            done, skipped = [], []
            for cid in ids:
                r = memory.review(cid, action if action == "discard" else "promote")
                (done if r else skipped).append(cid)
            return (f"✅ {action} 完成 {len(done)} 条"
                    + (f"；未找到/未变更 {len(skipped)} 条" if skipped else "")), None
        return help_text(), None

    def _fmt_status() -> str:
        lines = ["节点："]
        for n in registry.snapshot():
            info = n.get("info", {})
            lines.append(f"- {n['node_id']}: {n['status']}  harnesses={','.join(info.get('harnesses', []))}  {info.get('hostname', '')}")
        if not registry.nodes:
            lines.append("（无节点注册）")
        lines.append("最近任务：")
        for t in store.list_tasks(5):
            r = t.get("result") or {}
            out = (r.get("output") or r.get("error") or "")[:50].replace("\n", " ")
            lines.append(f"- {t['id']} [{t['status']}] {t.get('harness')}@{t.get('node_id')}: {out}")
        return "\n".join(lines)

    # ---------- 节点 WS 网关 ----------

    @app.websocket("/ws/node/{node_id}")
    async def node_ws(ws: WebSocket, node_id: str):
        token = ws.query_params.get("token", "")
        expect = cfg.node_tokens().get(node_id, "")
        if not expect or not hmac.compare_digest(token, expect):
            await ws.close(code=4401)
            return
        await ws.accept()
        try:
            first = await ws.receive_json()
        except WebSocketDisconnect:
            return
        if first.get("type") != P.T_NODE_ONLINE:
            await ws.close(code=4400, reason="first message must be node.online")
            return
        old = registry.nodes.get(node_id)
        if old is not None and old is not getattr(ws.state, "ns", None):
            try:
                await old.ws.close(code=4400, reason="replaced by new connection")
            except Exception:
                pass
        ns = registry.register(node_id, first.get("payload", {}), ws)
        ws.state.ns = ns
        store.add_event(first)
        try:
            while True:
                env = await ws.receive_json()
                await _dispatch(env, ns)
        except WebSocketDisconnect:
            pass
        finally:
            registry.unregister(node_id, expected=ns)

    async def _dispatch(env: dict, ns):
        t = env.get("type", "")
        if t in (P.T_HEARTBEAT, P.T_NODE_STATUS, P.T_TASK_ACCEPTED, P.T_TASK_RUNNING,
                 P.T_TASK_PROGRESS, P.T_TASK_RESULT, P.T_TASK_FAILED):
            registry.touch(ns.node_id)
        if t == P.T_MEMORY_CANDIDATE:
            store.add_memory_candidate(env.get("payload", {}), env.get("task_id"))
        if env.get("task_id") and t.startswith("task."):
            await tm.on_event(env)
        store.add_event(env)

    # ---------- 飞书 channel（默认关闭；启用前须处理与旧桥的互斥，见 feishu.py 注释）----------

    if os.getenv("AF_FEISHU_ENABLED") == "1":
        try:
            from .feishu import FeishuChannel
            ch = FeishuChannel(on_text=lambda text, open_id: handle_user_text(text))
            if ch.enabled:
                ch.start()
                hub.add(ch)
                print("[central] 飞书长连接 channel 已启动", flush=True)
        except Exception as e:
            print(f"[central] 飞书 channel 启动失败（忽略，继续无飞书运行）: {e!r}", flush=True)

    # ---------- 心跳巡检（Syne 模式：心跳过期判离线）----------

    async def _sweep_loop():
        while True:
            try:
                registry.sweep()
            except Exception:
                pass
            await asyncio.sleep(10)

    @app.on_event("startup")
    async def _startup():
        app.state.sweep_task = asyncio.get_running_loop().create_task(_sweep_loop())

    return app


def main():
    import uvicorn
    uvicorn.run(create_app(), host=cfg.host(), port=cfg.port(), log_level="info")


if __name__ == "__main__":
    main()
