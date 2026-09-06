"""codex 适配器（V0.12）：`codex exec` 非交互模式。

codex exec：给 prompt → 执行（默认沙箱）→ 输出最终消息 → 退出。
登录态 ~/.codex/auth.json（ChatGPT 账号，随配置复制到节点）。

环境：
  AF_CODEX_BIN     二进制路径（PATH 找不到时必须指定）
  AF_CODEX_TIMEOUT 整体超时秒数（默认 420）
"""
from __future__ import annotations

import asyncio
import os
import shutil

from .base import AdapterResult, HarnessAdapter, RunContext
from .opencode import _compose_prompt

DEFAULT_TIMEOUT = 420


def _bin() -> str | None:
    return os.getenv("AF_CODEX_BIN") or shutil.which("codex")


class CodexAdapter(HarnessAdapter):
    name = "codex"

    def __init__(self):
        self._proc = None

    async def start(self, goal: str, ctx: RunContext, progress) -> AdapterResult:
        g = goal.strip()
        binp = _bin()
        if not binp:
            return AdapterResult(False, "codex 二进制未找到：设置 AF_CODEX_BIN")
        try:
            timeout = int(os.getenv("AF_CODEX_TIMEOUT", "") or DEFAULT_TIMEOUT)
        except ValueError:
            timeout = DEFAULT_TIMEOUT

        if g == "probe":
            proc = await asyncio.create_subprocess_exec(
                binp, "--version",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=60)
            ver = out.decode(errors="replace").strip()[:200]
            return AdapterResult(True, f"codex {ver}\nauth=~/.codex（config.toml 指定 provider）")

        prompt = _compose_prompt(g, ctx)
        await progress(f"$ codex exec（prompt {len(prompt)} 字）")
        proc = await asyncio.create_subprocess_exec(
            binp, "exec", "--skip-git-repo-check", prompt,
            cwd=ctx.workspace,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        self._proc = proc
        out: list[str] = []
        try:
            while True:
                line = await asyncio.wait_for(proc.stdout.readline(), timeout=timeout)
                if not line:
                    break
                s = line.decode(errors="replace").rstrip()
                out.append(s)
                if len(out) % 40 == 0:  # codex 输出很长，节流推进度
                    await progress(s)
            rc = await proc.wait()
        except asyncio.TimeoutError:
            proc.kill()
            return AdapterResult(False, f"codex exec 超时（{timeout}s 无输出），已强杀\n"
                              + "\n".join(out[-20:]))
        finally:
            self._proc = None
        text = "\n".join(out)
        return AdapterResult(rc == 0, text or "(空输出)")

    async def cancel(self):
        if self._proc and self._proc.returncode is None:
            self._proc.kill()
