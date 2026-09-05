"""af/1 线协议：中央↔节点 控制面事件。

原则（见 docs/PLAN.md §6）：高频小消息走 WebSocket 控制面；
大文件/patch/长 transcript 走数据面（HTTP/Git/对象存储），绝不塞本协议。

Envelope 统一信封，type 见下方常量。payload 为宽松 dict（向前兼容），
关键字段在 TaskStartPayload / TaskResultPayload 等模型中约定。
"""
from __future__ import annotations

import time
import uuid

from pydantic import BaseModel, Field

PROTOCOL = "af/1"

# ---- central → node ----
T_NODE_CONFIGURE = "node.configure"
T_TASK_START = "task.start"
T_TASK_MESSAGE = "task.message"
T_TASK_CANCEL = "task.cancel"

# ---- node → central ----
T_NODE_ONLINE = "node.online"
T_NODE_STATUS = "node.status"
T_HEARTBEAT = "heartbeat"
T_TASK_ACCEPTED = "task.accepted"
T_TASK_RUNNING = "task.running"
T_TASK_PROGRESS = "task.progress"
T_TASK_RESULT = "task.result"
T_TASK_FAILED = "task.failed"
T_MEMORY_CANDIDATE = "memory.candidate"

TERMINAL_TASK_TYPES = {T_TASK_RESULT, T_TASK_FAILED}


class Envelope(BaseModel):
    v: str = PROTOCOL
    type: str
    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:12])
    ts: float = Field(default_factory=time.time)
    seq: int = 0
    task_id: str | None = None
    node_id: str | None = None
    payload: dict = Field(default_factory=dict)


def make(type_: str, payload: dict | None = None, **kw) -> dict:
    """构造一个信封 dict（transport 形态）。"""
    return Envelope(type=type_, payload=payload or {}, **kw).model_dump()


class NodeOnlinePayload(BaseModel):
    node_id: str
    hostname: str
    os: str
    python: str
    harnesses: list[str] = Field(default_factory=list)
    capabilities: dict = Field(default_factory=dict)
    workspace: str = ""


class TaskStartPayload(BaseModel):
    task_id: str
    goal: str
    harness: str
    workspace: str | None = None
    model_profile: str | None = None
    context_package: dict | None = None


class TaskResultPayload(BaseModel):
    task_id: str
    ok: bool
    output: str = ""
    artifacts: list[str] = Field(default_factory=list)


class MemoryCandidatePayload(BaseModel):
    kind: str = "experience"  # fact / lesson / decision / preference / experience
    content: str
    source_task: str | None = None
    importance: int = 1
