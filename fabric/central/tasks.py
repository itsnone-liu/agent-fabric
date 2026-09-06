"""任务管理：创建→派发→事件回流→完成；终态幂等（断线重放 outbox 不会重复结算）。"""
from __future__ import annotations

import time
import uuid

from ..shared import protocol as P
from .channels import ChannelHub
from .registry import NodeRegistry
from .store import Store

TERMINAL = {"done", "failed", "canceled"}


class TaskManager:
    def __init__(self, store: Store, registry: NodeRegistry, hub: ChannelHub, memory=None):
        self.store = store
        self.registry = registry
        self.hub = hub
        self.memory = memory  # MemoryManager，可选；用于组装 context_package

    def create(self, goal: str, harness: str, node_id: str | None = None) -> dict:
        task = {
            "id": "T-" + uuid.uuid4().hex[:6],
            "goal": goal, "harness": harness, "node_id": node_id,
            "status": "pending", "result": None,
            "created_at": time.time(), "updated_at": time.time(),
        }
        self.store.save_task(task)
        return task

    async def resume(self, prev_id: str, node_id: str | None = None,
                     harness: str | None = None, extra_goal: str = "") -> tuple[dict | None, str]:
        """任务可续：取上一轮 handoff（文件+输出尾部），组装续跑任务派发到任意节点。"""
        prev = self.store.get_task(prev_id)
        if prev is None:
            return None, f"任务 {prev_id} 不存在"
        pr = prev.get("result") or {}
        handoff = pr.get("handoff") or {}
        files = handoff.get("files") or {}
        tail = (handoff.get("tail") or pr.get("output") or "")[-500:]
        goal = (f"继续任务 {prev_id}（原目标：{prev['goal'][:200]}）。\n"
                f"[前情提要·上一轮输出尾部]\n{tail}\n"
                "[续跑说明] 上一轮工作区文件已恢复到本节点，请核验后在此基础上继续完成原目标，不要从零重做。")
        if extra_goal:
            goal += f"\n[追加指示] {extra_goal}"
        task = {
            "id": "T-" + uuid.uuid4().hex[:6],
            "goal": goal, "harness": harness or prev.get("harness", "echo"),
            "node_id": node_id, "status": "pending", "result": None,
            "parent_task": prev_id, "restore_files": files or None,
            "created_at": time.time(), "updated_at": time.time(),
        }
        self.store.save_task(task)
        msg = await self.dispatch(task)
        note = f"（handoff：{len(files)} 个文件恢复）" if files else "（上一轮无 handoff 文件，仅带前情提要续跑）"
        return task, msg + note

    async def dispatch(self, task: dict) -> str:
        ns = self.registry.pick(harness=task["harness"], node_id=task.get("node_id"))
        if ns is None:
            task.update(status="failed", updated_at=time.time(),
                        result={"ok": False, "output": f"无可调度节点：harness={task['harness']} node={task.get('node_id')}"})
            self.store.save_task(task)
            return f"❌ {task['id']} 派发失败：没有在线节点满足条件（harness={task['harness']}）"

        task["node_id"] = ns.node_id
        task["status"] = "dispatched"
        task["updated_at"] = time.time()
        self.store.save_task(task)

        ctx_pkg = None
        if self.memory is not None:
            try:
                ctx_pkg = self.memory.build_context_package(task["goal"], task_id=task["id"])
            except Exception as e:  # 记忆组装失败不阻塞任务
                ctx_pkg = {"warning": f"context package build failed: {e!r}"}

        env = P.make(P.T_TASK_START, {
            "task_id": task["id"], "goal": task["goal"], "harness": task["harness"],
            "context_package": ctx_pkg,
            "restore_files": task.get("restore_files"),
        }, task_id=task["id"], node_id=ns.node_id)
        try:
            await ns.ws.send_json(env)
        except Exception:
            ns.status = "offline"
            task.update(status="failed", updated_at=time.time(),
                        result={"ok": False, "output": f"下发失败：节点 {ns.node_id} 连接中断"})
            self.store.save_task(task)
            return f"❌ {task['id']} 下发失败（节点 {ns.node_id} 连接中断）"
        return f"✅ {task['id']} 已派发 → {ns.node_id} / {task['harness']}：{task['goal'][:60]}"

    async def on_event(self, env: dict):
        tid = env.get("task_id")
        task = self.store.get_task(tid) if tid else None
        if task is None or task.get("status") in TERMINAL:
            return  # 幂等：outbox 重放不重复结算
        t, pl = env.get("type", ""), env.get("payload", {})
        if t == P.T_TASK_ACCEPTED:
            self._set(task, "accepted")
        elif t == P.T_TASK_RUNNING:
            self._set(task, "running")
        elif t == P.T_TASK_PROGRESS:
            pass  # progress 明细走 events 表
        elif t == P.T_TASK_RESULT:
            ok = bool(pl.get("ok"))
            task["result"] = {"ok": ok, "output": pl.get("output", ""), "artifacts": pl.get("artifacts", []),
                              "handoff": pl.get("handoff")}
            # 竞态兜底：节点侧 canceled 标志可能因毫秒级并发丢失；中央看到
            # canceling 状态下的失败结果，同样认定被取消（幂等，不影响正常失败）
            if not ok and task.get("status") == "canceling":
                task["result"]["canceled"] = True
                task["result"]["output"] = "（已取消）\n" + str(task["result"].get("output", ""))[:500]
            self._set(task, "done" if ok else "failed")
            await self.hub.broadcast(self._fmt_result(task))
        elif t == P.T_TASK_FAILED:
            task["result"] = {"ok": False, "output": pl.get("error", "")}
            self._set(task, "failed")
            await self.hub.broadcast(f"❌ {tid} 失败：{str(pl.get('error', ''))[:300]}")

    async def cancel(self, task_id: str) -> str:
        task = self.store.get_task(task_id)
        if task is None:
            return f"任务 {task_id} 不存在"
        if task.get("status") in TERMINAL:
            return f"任务 {task_id} 已是终态（{task['status']}）"
        ns = self.registry.nodes.get(task.get("node_id") or "")
        if ns and ns.status == "online":
            try:
                await ns.ws.send_json(P.make(P.T_TASK_CANCEL, {"reason": "user"}, task_id=task_id, node_id=ns.node_id))
            except Exception:
                pass
        self._set(task, "canceling")
        return f"已发送取消请求：{task_id}"

    def _set(self, task: dict, status: str):
        task["status"] = status
        task["updated_at"] = time.time()
        self.store.save_task(task)

    @staticmethod
    def _fmt_result(task: dict) -> str:
        r = task.get("result") or {}
        output = str(r.get("output", ""))
        tail = output[-600:] if len(output) > 600 else output
        mark = "✅" if r.get("ok") else "⚠️"
        return f"{mark} {task['id']} 完成（{task.get('harness')} @ {task.get('node_id')}）\n{tail}"
