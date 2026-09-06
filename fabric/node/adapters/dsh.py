"""dsh 适配器（V0.12）：`dsh --profile headless` 单发模式。

headless 是 dsh 内置 profile：一个任务 → 打印最终回复 → 退出（无流式进度）。
模型由 DSH_HOME/settings.yaml 的 agent-default-model 决定（凭据 .credentials.yaml
同在 DSH_HOME；节点服务用独立 DSH_HOME，与机器上可能的 dsh 实例完全隔离）。

环境：
  AF_DSH_BIN     二进制路径（PATH 找不到时必须指定）
  DSH_HOME       dsh 配置目录（settings.yaml + .credentials.yaml + skills/）
  AF_DSH_TIMEOUT 整体超时秒数（默认 300；订阅模型响应快，免费/慢模型可调大）
"""
from __future__ import annotations

import asyncio
import os
import shutil

from .base import AdapterResult, HarnessAdapter, RunContext
from .opencode import _compose_prompt

DEFAULT_TIMEOUT = 300


def _bin() -> str | None:
    return os.getenv("AF_DSH_BIN") or shutil.which("dsh")


class DshAdapter(HarnessAdapter):
    name = "dsh"

    def __init__(self):
        self._proc = None

    async def start(self, goal: str, ctx: RunContext, progress) -> AdapterResult:
        g = goal.strip()
        binp = _bin()
        if not binp:
            return AdapterResult(False, "dsh 二进制未找到：设置 AF_DSH_BIN")
        env = dict(os.environ)  # 继承服务的 DSH_HOME（systemd Environment= 注入）
        try:
            timeout = int(os.getenv("AF_DSH_TIMEOUT", "") or DEFAULT_TIMEOUT)
        except ValueError:
            timeout = DEFAULT_TIMEOUT

        if g == "probe":
            proc = await asyncio.create_subprocess_exec(
                binp, "--version", env=env,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=60)
            ver = out.decode(errors="replace").strip()[:200]
            home = env.get("DSH_HOME", "~/.dsh 默认")
            model = "settings:agent-default-model"
            return AdapterResult(True, f"dsh {ver}\nDSH_HOME={home}\nmodel={model}")

        prompt = _compose_prompt(g, ctx)
        await progress(f"$ dsh --profile headless（prompt {len(prompt)} 字）")
        proc = await asyncio.create_subprocess_exec(
            binp, "--profile", "headless", prompt,
            cwd=ctx.workspace, env=env,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        self._proc = proc
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            return AdapterResult(False, f"dsh headless 超时（{timeout}s），已强杀")
        finally:
            self._proc = None
        text = out.decode(errors="replace").strip()
        return AdapterResult(proc.returncode == 0, text or "(空输出)")

    async def cancel(self):
        if self._proc and self._proc.returncode is None:
            self._proc.kill()
