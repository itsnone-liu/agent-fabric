"""echo 适配器：垂直切片/探活/受限 shell。

  run echo probe          → 节点环境自检
  run echo !<cmd>         → 受限 shell（需节点 AF_ALLOW_SHELL=1，默认拒绝）
  run echo <其他文本>      → 原样回显
"""
from __future__ import annotations

import asyncio
import platform
import socket

from .base import AdapterResult, HarnessAdapter, RunContext

SHELL_TIMEOUT = 120


class EchoAdapter(HarnessAdapter):
    name = "echo"

    def __init__(self):
        self._proc = None

    async def start(self, goal: str, ctx: RunContext, progress) -> AdapterResult:
        g = goal.strip()
        if g == "probe":
            lines = [
                f"node={socket.gethostname()}",
                f"os={platform.platform()}",
                f"python={platform.python_version()}",
                f"workspace={ctx.workspace}",
                f"allow_shell={ctx.allow_shell}",
            ]
            for l in lines:
                await progress(l)
            return AdapterResult(True, "\n".join(lines))

        if g.startswith("!"):
            if not ctx.allow_shell:
                return AdapterResult(False, "shell 执行被拒绝：该节点未设置 AF_ALLOW_SHELL=1")
            cmd = g[1:]
            await progress(f"$ {cmd}")
            proc = await asyncio.create_subprocess_shell(
                cmd, cwd=ctx.workspace,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
            self._proc = proc
            out: list[str] = []
            try:
                while True:
                    line = await asyncio.wait_for(proc.stdout.readline(), timeout=SHELL_TIMEOUT)
                    if not line:
                        break
                    s = line.decode(errors="replace").rstrip()
                    out.append(s)
                    await progress(s)
                rc = await proc.wait()
            except asyncio.TimeoutError:
                proc.kill()
                return AdapterResult(False, "命令超时（120s），已强杀\n" + "\n".join(out[-20:]))
            finally:
                self._proc = None
            return AdapterResult(rc == 0, f"exit={rc}\n" + "\n".join(out))

        return AdapterResult(True, f"echo[{socket.gethostname()}] {goal}")

    async def cancel(self):
        if self._proc and self._proc.returncode is None:
            self._proc.kill()
