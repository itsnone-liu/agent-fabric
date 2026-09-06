"""opencode 适配器（V0：probe + 非交互 run；V1 换 ACP 会话，参考 agent-gateway 的
`opencode acp --cwd <cwd>` 启动方式与 codex-acp adapter）。

环境：
  AF_OPENCODE_BIN     二进制路径（PATH 找不到时必须指定）
  AF_OPENCODE_MODEL   模型 id（如 opencode/mimo-v2.5-free），缺省用 opencode 配置默认
  AF_OPENCODE_TIMEOUT 单行输出超时秒数（默认 300；免费模型偏慢可调大）
"""
from __future__ import annotations

import asyncio
import os
import shutil

from .base import AdapterResult, HarnessAdapter, RunContext

DEFAULT_TIMEOUT = 300


def _bin() -> str | None:
    return os.getenv("AF_OPENCODE_BIN") or shutil.which("opencode")


def _timeout() -> int:
    try:
        return int(os.getenv("AF_OPENCODE_TIMEOUT", "") or DEFAULT_TIMEOUT)
    except ValueError:
        return DEFAULT_TIMEOUT


def _compose_prompt(goal: str, ctx: RunContext) -> str:
    """goal + 中央 context_package（记忆→任务链路，PLAN §12）。

    中央包形如 {"identity":..., "experience":[{kind,content,...}], ...}（V0 键名）；
    缺省/异常时静默退化为裸 goal，记忆组装问题不应让任务跑不起来。
    """
    pkg = getattr(ctx, "context_package", None)
    if not isinstance(pkg, dict):
        return goal
    mems = pkg.get("experience") or pkg.get("memories") or []
    if not mems:
        return goal
    lines = [f"[{m.get('kind', 'note')}] {str(m.get('content', ''))[:300]}" for m in mems[:10]]
    return ("<context>\n相关记忆（来自中央记忆库，供参考）：\n"
            + "\n".join(lines) + "\n</context>\n\n" + goal)


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
        prompt = _compose_prompt(g, ctx)
        args = [oc, "run"]
        if model:
            args += ["--model", model]
        args.append(prompt)
        await progress(f"$ opencode run（model={model or '配置默认'}，prompt {len(prompt)} 字）")
        proc = await asyncio.create_subprocess_exec(
            *args, cwd=ctx.workspace,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        self._proc = proc
        out: list[str] = []
        timeout = _timeout()
        try:
            while True:
                line = await asyncio.wait_for(proc.stdout.readline(), timeout=timeout)
                if not line:
                    break
                s = line.decode(errors="replace").rstrip()
                out.append(s)
                await progress(s)
            rc = await proc.wait()
        except asyncio.TimeoutError:
            proc.kill()
            return AdapterResult(False, f"opencode run 超时（{timeout}s 无输出），已强杀\n" + "\n".join(out[-20:]))
        finally:
            self._proc = None
        text = "\n".join(out)
        return AdapterResult(rc == 0, text or "(空输出)")

    async def cancel(self):
        if self._proc and self._proc.returncode is None:
            self._proc.kill()
