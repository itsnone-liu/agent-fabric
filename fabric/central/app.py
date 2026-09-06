"""中央控制器入口：WS 节点网关 + 调试/Channel REST + 飞书(可选)。

边界（PLAN §2/§3）：Channel 与 Memory 属于中央；节点不知道飞书存在。
"""
from __future__ import annotations

import asyncio
import hmac
import os
import threading

from fastapi import FastAPI, WebSocket, WebSocketDisconnect

from ..shared import config as cfg
from ..shared import protocol as P
from .channels import ChannelHub, ConsoleChannel
from .memory import MemoryManager
from .registry import NodeRegistry
from .router import Action, help_text, parse_command, KNOWN_HARNESSES
from .store import Store
from .tasks import TERMINAL, TaskManager


def create_app(db_path: str | None = None) -> FastAPI:
    store = Store(db_path or cfg.db_path())
    registry = NodeRegistry()
    hub = ChannelHub([ConsoleChannel()])
    memory = MemoryManager(store)
    tm = TaskManager(store, registry, hub, memory=memory)
    from .session import SessionManager
    session = SessionManager(online_nodes=lambda: {n["node_id"]: (n.get("info") or {})
                                                   for n in registry.snapshot()
                                                   if n.get("status") == "online"})
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
        if r and body.get("action") == "promote":
            # V0.10.1：promote 后自动判断蒸馏（fire-and-forget，不打断回复）
            seed = str(r.get("content") or "")[:200]
            asyncio.get_running_loop().create_task(_auto_crystallize_notify(seed))
        return r or {"error": "candidate not found"}

    async def _auto_ingest_notify(r: dict):
        """自动入库轻通知 + 自动结晶判断（更正 memory edit / 删除 memory forget）。"""
        try:
            await hub.broadcast(f"✅ 经验自动入库 {r.get('memory')}："
                                f"{str(r.get('content'))[:80]}"
                                "\n（更正：memory edit <id> <新内容>；删除：memory forget <id>）")
            await _auto_crystallize_notify(str(r.get("content") or "")[:200])
        except Exception as e:
            hub.console(f"[auto-ingest] {e!r}")

    async def _auto_crystallize_notify(seed: str):
        """自动蒸馏 + 结果通知（人可见可撤——不满意 memory forget）。"""
        try:
            focus = getattr(session, "focus", None)
            res = await memory.auto_crystallize(seed, tm=tm, node=None)  # 蒸馏无节点偏好，registry 自挑
            if res.get("triggered"):
                import re as _re
                draft = str(res.get("draft") or "")
                m = _re.search(r"【技能】(.+)", draft)
                title = (m.group(1).strip() if m else res.get("topic"))  # 优先用蒸馏自拟标题
                await hub.broadcast(
                    f"🧊 自动结晶技能 {res.get('skill_id')}（{res.get('n_sources')} 条经验"
                    f"·{res.get('method')}）：{title}\n"
                    + draft[:300]
                    + "\n（不满意 memory forget 该 id / 更正 memory edit 该 id <新内容>）")
        except Exception as e:
            hub.console(f"[auto-crystallize] {e!r}")

    @app.post("/api/improve")
    async def improve_cron():
        """自我审计（fabric-improve.timer 每日调；`improve` 命令同一逻辑）。"""
        from .selfimprove import run_audit
        focus = getattr(session, "focus", None)
        r = await run_audit(tm, memory, node=focus)
        if r.get("ok"):
            await hub.broadcast("🔍 每日自审报告（" + str(r.get("task_id")) + "）：\n"
                                + str(r["report"])[:900])
        return r

    @app.post("/api/dream")
    async def dream_cron():
        """外部 cron/timer 入口（V0.10.1：systemd timer 每 6h 调一次）。"""
        try:
            stat = memory.dream()
            return {"ok": True, **stat}
        except Exception as e:
            return {"ok": False, "error": repr(e)[:200]}

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
            nid, _ = session.resolve(act.node)
            msg = session.switch(act.node)
            avail = session.node_harnesses(nid or act.node)
            if act.harness and act.harness != "echo":  # use 汤圆 codex 显式指定
                session.harness = act.harness if act.harness in avail else None
                if not session.harness:
                    msg += f"\n⚠️ {nid or act.node} 没有 harness {act.harness}（可用：{'/'.join(avail) or '无'}）"
            else:
                session.harness = session.pick_harness(nid or act.node)
            if session.harness:
                msg += f"\nharness：{session.harness}" + \
                       (f"（可切换：{'/'.join(avail)}，说 harness <名>）" if len(avail) > 1 else "")
            return msg, None
        if act.kind == "model":
            m = (act.model or "").strip()
            if not m:  # 查询当前
                return (f"当前模型：{session.model or '（各 harness 默认）'}"
                        f"\nharness：{session.harness or '?'}@{session.focus or '?'}"
                        "\n切换：model <名字>（codex 传 -m；opencode 传 --model；dsh 由节点配置定）"), None
            session.model = m
            return f"✅ 模型已切：{m}（{session.harness or '?'}@{session.focus or '?'}；adapter 不支持的会忽略）", None
        if act.kind == "harness":
            h = (act.harness or "").strip()
            if h not in KNOWN_HARNESSES:
                return f"未知 harness {h}（已知：{'/'.join(sorted(KNOWN_HARNESSES - {'echo'}))}）", None
            if not session.focus:
                return "先 use 选主机，再切 harness", None
            avail = session.node_harnesses(session.focus)
            if h not in avail:
                return f"{session.focus} 没有 {h}（可用：{'/'.join(avail) or '无'}）", None
            session.harness = h
            return f"✅ harness 已切：{h}（{session.focus}）", None
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
            h = session.harness or "opencode"
            avail = session.node_harnesses(session.focus)
            if avail and h not in avail:  # 兜底：会话 harness 不在当前节点 → 自动换
                h = session.pick_harness(session.focus) or h
                session.harness = h
            task = tm.create(act.raw, h, session.focus, model=session.model)
            msg = await tm.dispatch(task)
            session.last_task = task["id"]
            return msg + "\n（免费模型节奏慢，一般 1~3 分钟；过程中会有 ⏳ 推送，期间说话=排队追加意见）", task["id"]
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
            focus = getattr(session, "focus", None) if session else None
            res = await memory.crystallize(act.goal, tm=tm, node=None)  # 同上：蒸馏任务跨节点挑
            if not res.get("ok"):
                return "🧊 " + res.get("hint", "素材不足"), None
            how = f"{res.get('method', '拼接')}·节点免费模型" if res.get("method") == "蒸馏" else "确定性拼接"
            return (f"🧊 已结晶技能 {res['skill_id']}（{res['n_sources']} 条经验·{how}）\n"
                    + res["draft"][:600]
                    + "\n（不满意 memory forget 该 id 后换关键词重试）"), None
        if act.kind == "improve":
            from .selfimprove import run_audit
            focus = getattr(session, "focus", None)
            r = await run_audit(tm, memory, node=focus)
            if r.get("ok"):
                return "🔍 自审报告（" + str(r.get("task_id")) + "）：\n" + str(r["report"])[:900] + \
                       "\n（人拍板后才动手改；满意与否都可直接说想法）", None
            return "⚠️ 自审未完成：" + str(r.get("hint")), None
        if act.kind == "dream":
            stat = memory.dream()
            return ("💤 dream 整理完成：task 流水清理 {} 条、相似合并 {} 组、"
                    "零命中降权 {} 条".format(stat["task_trimmed"], stat["merged"], stat["demoted"])), None
        if act.kind == "memory_edit":
            parts = act.goal.strip().split(None, 1)
            if len(parts) < 2 or not parts[0].startswith("M-"):
                return "用法：memory edit <M-id> <新内容>", None
            r = memory.edit_memory(parts[0], parts[1])
            return (f"✏️ 已更正 {parts[0]}：{parts[1][:80]}"
                    if r.get("ok") else "❌ " + r.get("hint", "未找到")), None
        if act.kind == "memory_forget":
            ok = memory.store.delete_memory(act.goal.strip().split()[0]) if act.goal.strip() else False
            return ("🗑️ 已删除记忆 " + act.goal.strip() if ok
                    else "❌ 未找到该记忆 id（memories 面板可查）"), None
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
            from .reply_clean import extract_reply
            body = extract_reply(str(r.get("output") or "")) or (r.get("output") or r.get("error") or "")[-160:]
            lines = [f"{t['id']} {t['status']} @{t.get('node_id')} {t.get('harness')}",
                     f"目标: {(t.get('goal') or '')[:120]}",
                     f"回复: {body}"]
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
                if r and action == "promote":  # V0.10.1 自动蒸馏（同 REST 路径）
                    asyncio.get_running_loop().create_task(
                        _auto_crystallize_notify(str(r.get("content") or "")[:200]))
            return (f"✅ {action} 完成 {len(done)} 条"
                    + (f"；未找到/未变更 {len(skipped)} 条" if skipped else "")
                    + ("；素材够会自动结晶（稍候推送）" if action == "promote" and done else "")), None
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
            r = memory.ingest_candidate(env.get("payload", {}), env.get("task_id"))
            if r.get("auto_promoted"):
                asyncio.get_running_loop().create_task(_auto_ingest_notify(r))
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
