"""opencode 适配器（V0：probe + 非交互 run；V1 换 ACP 会话，参考 agent-gateway 的
`opencode acp --cwd <cwd>` 启动方式与 codex-acp adapter）。

环境：
  AF_OPENCODE_BIN   二进制路径（PATH 找不到时必须指定）
  AF_OPENCODE_MODEL 模型 id（如 bailian/deepseek-v4-flash-0731），缺省用 opencode 配置默认
"""
from __future__ import annotations

import asyncio
import os
import shutil

from .base import AdapterResult, HarnessAdapter, RunContext

RUN_TIMEOUT = 300


def _bin() -> str | None:
    return os.getenv("AF_OPENCODE_BIN") or shutil.which("opencode")


class OpenCodeAdapter(HarnessAdapter):
    name = "opencode"

    def __init__(self):
        self._proc = None

    async def start(self, goal: str, ctx: RunContext, progress) -> AdapterResult:
        g = goal.strip()
        oc = _bin()
        if not oc:
            return AdapterResult(False, "opencode 二进制未找到：设置 AF_OPENCODE_BIN")
        model = os.getenv("AF_OPENCODE_MODEL", "").strip()

        if g == "probe":
            proc = await asyncio.create_subprocess_exec(
                oc, "--version", cwd=ctx.workspace,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=60)
            ver = out.decode(errors="replace").strip()
            await progress(ver)
            where = f"（binary: {oc}）"
            return AdapterResult(True, f"opencode {ver} {where}\nmodel={'AF_OPENCODE_MODEL=' + model if model else '配置默认'}")

        # 普通任务：非交互 run（V1 换 ACP 长会话 + handoff）
        args = [oc, "run"]
        if model:
            args += ["--model", model]
        args.append(g)
        await progress(f"$ {' '.join(args[:4])} …")
        proc = await asyncio.create_subprocess_exec(
            *args, cwd=ctx.workspace,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        self._proc = proc
        out: list[str] = []
        try:
            while True:
                line = await asyncio.wait_for(proc.stdout.readline(), timeout=RUN_TIMEOUT)
                if not line:
                    break
                s = line.decode(errors="replace").rstrip()
                out.append(s)
                await progress(s)
            rc = await proc.wait()
        except asyncio.TimeoutError:
            proc.kill()
            return AdapterResult(False, "opencode run 超时（300s），已强杀\n" + "\n".join(out[-20:]))
        finally:
            self._proc = None
        text = "\n".join(out)
        return AdapterResult(rc == 0, text or "(空输出)")

    async def cancel(self):
        if self._proc and self._proc.returncode is None:
            self._proc.kill()
