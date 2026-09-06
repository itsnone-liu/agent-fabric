"""任务管理：创建→派发→事件回流→完成；终态幂等（断线重放 outbox 不会重复结算）。"""
from __future__ import annotations

import time
import uuid

from ..shared import protocol as P
from .channels import ChannelHub
from .memory import _clean as _ansi
from .registry import NodeRegistry
from .store import Store

TERMINAL = {"done", "failed", "canceled"}


class TaskManager:
    def __init__(self, store: Store, registry: NodeRegistry, hub: ChannelHub, memory=None):
        self.store = store
        self.registry = registry
        self.hub = hub
        self.memory = memory  # MemoryManager，可选；用于组装 context_package
        self.session = None            # app 装配后注入（V0.9 会话化）
        self._prog_last = 0.0          # 上次过程推送时间（全局节流窗）
        self._prog_buf: list[str] = []  # 窗口内关键行

    def create(self, goal: str, harness: str, node_id: str | None = None,
               internal: bool = False) -> dict:
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
                ctx_pkg = self.memory.build_context_package(
                    task["goal"], task_id=task["id"], parent_id=task.get("parent_task"))
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
            if not task.get("internal"):
                await self._push_progress_throttled(task, pl)  # 精简过程推送（V0.9）
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
            if self.memory is not None:
                try:
                    self.memory.on_task_result(task)  # task级记忆自动入库（零审核）
                except Exception:
                    pass
            if not task.get("internal"):
                await self.hub.broadcast(self._fmt_result(task))
                await self._auto_followup(task)  # 排队的追加意见 → 自动续跑（V0.9）
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

    # ---- V0.9 会话化：过程节流推送 / 结果完整化 / 自动续跑 ----
    _PROGRESS_KEYS = ("Write", "Wrote", "Edit", "Bash", "Read", "Run", "Error", "error", "失败",
                      "成功", "完成", "创建", "生成", "运行", "✓", "✅", "❌")

    async def _push_progress_throttled(self, task: dict, pl: dict):
        """过程精简推送：仅关键行、8s 时间窗合并（明细仍全量在 events 表）。

        opencode 实际行格式（2026-09-06 实测）：`$ cmd`（命令）、`→ Read f`
        （工具）、裸路径输出——英文工具名词表（Write/Bash）匹配不上 `$ pwd`，
        必须按行形态识别。命令行（$ 开头）高价值，立即推不聚合。
        """
        import time as _t
        line = _ansi(str(pl.get("text", ""))).strip()
        if not line or len(line) < 4:
            return
        is_cmd = line.startswith("$ ") or line.startswith("→ ")
        if not (is_cmd or any(k in line for k in self._PROGRESS_KEYS)):
            return  # 非关键行不推（用户要过程精简）
        if is_cmd:  # 命令/工具行即时推，不让用户在静默里等
            self._prog_buf.append(f"{task['id']} · {line[:110]}")
            self._prog_last = _t.time()
            chunk, self._prog_buf = self._prog_buf[-6:], []
            await self.hub.broadcast("⏳ 过程：\n" + "\n".join(chunk))
            return
        self._prog_buf.append(f"{task['id']} · {line[:110]}")
        now = _t.time()
        if now - self._prog_last < 8 and len(self._prog_buf) < 6:
            return
        self._prog_last = now
        chunk, self._prog_buf = self._prog_buf[-6:], []
        await self.hub.broadcast("⏳ 过程：\n" + "\n".join(chunk))

    async def _auto_followup(self, task: dict):
        """任务终态后，把会话中排队的追加意见自动续跑（resume 链）。"""
        sess = getattr(self, "session", None)
        if not sess or not sess.followups:
            return
        extra = "；".join(sess.followups)
        sess.followups = []
        try:
            new_task, msg = await self.resume(task["id"], node_id=sess.focus,
                                              harness=task.get("harness") or "opencode",
                                              extra_goal=extra)
            if new_task:
                sess.last_task = new_task["id"]
                await self.hub.broadcast(f"📋 已按你的追加意见自动续跑 → {new_task['id']}（恢复工作区+意见：{extra[:120]}）")
        except Exception as e:
            await self.hub.broadcast(f"⚠️ 自动续跑失败：{e!r}（可手动 resume {task['id']}）")

    @staticmethod
    def _fmt_result(task: dict) -> str:
        r = task.get("result") or {}
        output = str(r.get("output", ""))
        # 整理成模型的完整自然回复（用户 2026-09-06）：剥过程符号、保全正文
        # 数字/代码块；提取失败退回原始尾部——宁可多给不可丢答案。
        from .reply_clean import extract_reply
        body = extract_reply(output)
        if body is None:
            body = output[-1200:]
        elif len(body) > 2500:
            body = body[-2500:]
        mark = "✅" if r.get("ok") else "⚠️"
        files = list(((r.get("handoff") or {}).get("files") or {}))
        packed = f"\n📦 工作区：{', '.join(files[:10])}" + (f" 等{len(files)}个文件" if len(files) > 10 else "") if files else ""
        return f"{mark} {task['id']} 完成（{task.get('harness')} @ {task.get('node_id')}）{packed}\n{body}"
