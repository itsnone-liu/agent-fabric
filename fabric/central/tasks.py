"""任务管理：创建→派发→事件回流→完成；终态幂等（断线重放 outbox 不会重复结算）。"""
from __future__ import annotations

import json
import os
import time
import uuid
import asyncio

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
               internal: bool = False, model: str | None = None) -> dict:
        task = {
            "id": "T-" + uuid.uuid4().hex[:6],
            "goal": goal, "harness": harness, "node_id": node_id,
            "status": "pending", "result": None, "model": model,
            "internal": bool(internal),
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
        prev_sem = (pr.get("semantic") or (prev.get("result") or {}).get("semantic"))
        if prev_sem:  # Semantic Handoff：结构化交接优先于输出尾部
            goal += f"\n[上一轮交接（结构化）]\n{render_handoff(prev_sem)}"
        if prev.get("cancel_reason"):  # V2a：中断原因注入——agent 知道为什么被打断
            goal += f"\n[用户中断原因] {str(prev['cancel_reason'])[:300]}\n（上次因此被中断，务必调整）"
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

        # V1.2 Task/Run 分离：dispatch = 建 run（harness+model 对 task 的一次执行）
        run = {"id": "R-" + uuid.uuid4().hex[:6], "task_id": task["id"],
               "harness": task["harness"], "model": task.get("model"),
               "status": "running", "started_at": time.time()}
        self.store.save_run(run)
        task["last_run"] = run["id"]
        env = P.make(P.T_TASK_START, {
            "task_id": task["id"], "run_id": run["id"],
            "goal": task["goal"], "harness": task["harness"],
            "context_package": ctx_pkg,
            "internal": bool(task.get("internal")),
            "model": task.get("model"),
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
                              "handoff": pl.get("handoff"),
                              "semantic": parse_handoff(str(pl.get("output", "")) or "")}
            # 竞态兜底：节点侧 canceled 标志可能因毫秒级并发丢失；中央看到
            # canceling 状态下的失败结果，同样认定被取消（幂等，不影响正常失败）
            if not ok and task.get("status") == "canceling":
                task["result"]["canceled"] = True
                task["result"]["output"] = "（已取消）\n" + str(task["result"].get("output", ""))[:500]
            # V1.2：结果落 run 行，task 终态由 run 派生
            rid = env.get("payload", {}).get("run_id") or task.get("last_run")
            if rid:
                try:
                    self.store.update_run(rid, status="done" if ok else "failed",
                                          ended_at=time.time(),
                                          result=json.dumps(task.get("result"), ensure_ascii=False)
                                          if task.get("result") else None)
                except Exception:
                    pass
            self._set(task, "done" if ok else "failed")
            if not ok:
                # 配额耗尽不是普通失败：同一轮自动切 codex，并恢复 handoff，
                # 避免用户必须手动发送“继续”。仅 dsh→codex，codex失败不再递归切换。
                await self._auto_quota_failover(task)
            elif task.get("harness") == "codex":
                # codex 接管后，等待原 dsh 配额窗口恢复，再自动回切 glm/dsh。
                await self._schedule_dsh_return(task)
            # V1.1 State/Memory 分离：task 流水不再进 memories 表（State 留
            # tasks 表；resume 父记忆改读 tasks 表——见 memory.build_context_package）
            if not task.get("internal"):
                await self.hub.broadcast(self._fmt_result(task))
                await self._auto_followup(task)  # 排队的追加意见 → 自动续跑（V0.9）
        elif t == P.T_TASK_FAILED:
            task["result"] = {"ok": False, "output": pl.get("error", "")}
            self._set(task, "failed")
            await self.hub.broadcast(f"❌ {tid} 失败：{str(pl.get('error', ''))[:300]}")

    async def retry(self, task_id: str, harness: str | None = None,
                    model: str | None = None) -> tuple[dict | None, str]:
        """V1.2：失败任务重跑——同 task 新 run（可换 harness/model："换 codex 再试"）。"""
        task = self.store.get_task(task_id)
        if task is None:
            return None, f"任务 {task_id} 不存在"
        if task.get("status") not in TERMINAL:
            return None, f"任务 {task_id} 还在跑（{task['status']}），先 cancel"
        if harness:
            task["harness"] = harness
        if model:
            task["model"] = model
        prev_sem = (task.get("result") or {}).get("semantic")
        if prev_sem:  # Semantic Handoff：上一 run 的交接块注入（GPT §12）
            task["goal"] = (f"{task['goal']}\n\n[上一 run 交接（{task['harness']} 执行）]\n"
                            f"{render_handoff(prev_sem)}")
        task["status"] = "pending"
        task["updated_at"] = time.time()
        self.store.save_task(task)
        msg = await self.dispatch(task)
        return task, msg

    async def send_message(self, task_id: str, text: str) -> str:
        """V2a：任务运行中插话 → T_TASK_MESSAGE → 节点写 inbox.md。"""
        task = self.store.get_task(task_id)
        if task is None:
            return ""
        ns = self.registry.nodes.get(task.get("node_id") or "")
        if ns and ns.status == "online":
            try:
                await ns.ws.send_json(P.make(P.T_TASK_MESSAGE,
                    {"text": text[:2000]}, task_id=task_id, node_id=ns.node_id,
                    run_id=task.get("last_run")))
                return "已插话"
            except Exception:
                pass
        return ""

    async def cancel(self, task_id: str, reason: str = "") -> str:
        """V2a：cancel 可带原因——kill 照旧（急中断），原因存 task 供 resume 注入
        （"为什么被打断"在续跑时 agent 才知道，避免再犯）。"""
        task = self.store.get_task(task_id)
        if task is None:
            return f"任务 {task_id} 不存在"
        if task.get("status") in TERMINAL:
            return f"任务 {task_id} 已是终态（{task['status']}）"
        ns = self.registry.nodes.get(task.get("node_id") or "")
        if ns and ns.status == "online":
            try:
                await ns.ws.send_json(P.make(P.T_TASK_CANCEL,
                    {"reason": reason or "user", "run_id": task.get("last_run")},
                    task_id=task_id, node_id=ns.node_id))
            except Exception:
                pass
        if reason:
            task["cancel_reason"] = reason[:500]
            self.store.save_task(task)
        self._set(task, "canceling")
        return f"已发送取消请求：{task_id}" + ("（原因已记录，resume 时注入）" if reason else "")

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

    async def _schedule_dsh_return(self, task: dict):
        """codex 轮完成后延迟自动回 dsh；下一轮任务会继续使用 glm 配置。"""
        delay = float(os.getenv("AF_QUOTA_RETURN_SECONDS", "18000"))
        await self.hub.broadcast(f"⏳ codex 已完成，{int(delay/3600)}小时后自动切回 dsh(glm)；无需人工继续")
        async def later():
            await asyncio.sleep(delay)
            try:
                new_task, _ = await self.resume(task["id"], node_id=task.get("node_id"),
                                                harness="dsh",
                                                extra_goal="配额窗口已恢复，自动切回 dsh(glm)；请继续完成原目标，保留 codex 已完成内容。")
                if new_task:
                    await self.hub.broadcast(f"🔄 dsh(glm) 配额窗口恢复，已自动切回续跑 → {new_task['id']}")
            except Exception as e:
                await self.hub.broadcast(f"⚠️ 自动切回 dsh 失败：{e!r}；可手动 resume {task['id']}")
        asyncio.create_task(later())

    async def _auto_quota_failover(self, task: dict):
        """配额失败自动切 codex；codex 失败或非配额失败保持原终态。"""
        if task.get("harness") != "dsh" or not task.get("auto_resume", True):
            return
        text = str((task.get("result") or {}).get("output") or "")
        quota = any(x in text.lower() for x in ("1308", "quota", "usage limit", "达到5小时", "额度", "配额"))
        if not quota:
            return
        try:
            task["harness"] = "codex"
            task["status"] = "pending"
            task["resume_after"] = time.time()
            self.store.save_task(task)
            new_task, msg = await self.resume(task["id"], node_id=task.get("node_id"),
                                              harness="codex",
                                              extra_goal="上一模型触发配额限制，已自动切换 codex；请从交接状态继续，不要重做已完成工作。")
            if new_task:
                await self.hub.broadcast(f"🔁 检测到模型额度耗尽，已自动切换 codex 续跑 → {new_task['id']}")
        except Exception as e:
            await self.hub.broadcast(f"⚠️ 自动切 codex 失败：{e!r}；可手动 resume {task['id']}")

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


def parse_handoff(output: str) -> dict | None:
    """从 run 输出尾部解析 [HANDOFF] 自报块（Semantic Handoff §12/26）。
    格式：[HANDOFF] 后跟 成功:/决定:/待办: 行；解析失败返回 None（调用方回退尾部文本）。"""
    import re
    m = re.search(r"\[HANDOFF\]\s*\n(.+?)\s*(?:\n\[LESSON\]|\Z)", output, re.S)
    if not m:
        return None
    blocks = {"completed": [], "decisions": [], "pending": []}
    for line in m.group(1).splitlines():
        line = line.strip().lstrip("-*•").strip()
        for key, tag in (("completed", "完成"), ("decisions", "决定"), ("pending", "待办")):
            if line.startswith(tag) and len(line) > len(tag) + 1:
                blocks[key].append(line.split(":", 1)[1].strip()[:120])
    if not any(blocks.values()):
        return None
    return blocks


def render_handoff(h: dict) -> str:
    parts = []
    if h.get("completed"):
        parts.append("已完成：\n" + "\n".join(f"- {x}" for x in h["completed"]))
    if h.get("decisions"):
        parts.append("关键决定：\n" + "\n".join(f"- {x}" for x in h["decisions"]))
    if h.get("pending"):
        parts.append("待办（请接续完成，勿重做已完成部分）：\n" + "\n".join(f"- {x}" for x in h["pending"]))
    return "\n".join(parts)
