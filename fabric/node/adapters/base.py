"""HarnessAdapter 抽象（PLAN §8/§32，分层参考 agent-gateway 的 runtime/agent-SPI 拆分）。

V0 有意砍掉 checkpoint()/transcript() 一等公民（DECISIONS #3）：
跨节点续任务走 handoff package + git（V1），先保证 start/cancel/status 可靠。
"""
from __future__ import annotations

import abc
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable


@dataclass
class AdapterResult:
    ok: bool
    output: str = ""
    artifacts: list[str] = field(default_factory=list)


@dataclass
class RunContext:
    workspace: Path
    allow_shell: bool = False
    env: dict = field(default_factory=dict)


ProgressFn = Callable[[str], Awaitable[None]]


class HarnessAdapter(abc.ABC):
    """统一 harness 接口：中央不知道 opencode 是 ACP、codex 是 app-server、DSH 是 CLI。"""

    name: str = "base"

    @abc.abstractmethod
    async def start(self, goal: str, ctx: RunContext, progress: ProgressFn) -> AdapterResult:
        ...

    async def cancel(self):
        """尽力取消当前运行（强杀语义由各 adapter 决定）。"""
