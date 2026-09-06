"""结果正文提取：从 opencode 会话输出中剥出模型的最终自然回复。

原则（用户 2026-09-06）：
  · 过程符号行（> build / $ cmd / ←✱→ 工具行 / Wrote file… 状态行）剥掉
  · 正式回复里的数字、货币 $、代码块 ```（含行首 $ 示例、# 注释）一律保留
策略：从尾部向前扫描收集正文，整行匹配过程模式才剥（不做字符级删除，
不碰 fence 内内容）；提取失败或过短则退回原始尾部——宁可多给不可丢答案。
"""
from __future__ import annotations

import re

from .memory import _clean as _ansi

_ANSI2 = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")

_PROC_LINE = re.compile(
    r"""^(?:
        >\s*build\b            # 会话头
      | [←✱→✓✔✗❯]\s*           # opencode 工具符号行
      | \$\s+\S               # 命令执行行（行首 $ + 空格 + 命令）
      | Wrote\s+file\b
      | Wrote\s+\d+\s+files\b
      | Ran\s+\d+\s+(tool|tools)\b
      | No\s+edits?\s+made\b
      | \x1b\[0m\s*$
      | \[LESSON\]\s*            # 给记忆系统的标注行，不进用户回复
    )""", re.VERBOSE)

_FENCE = "```"


def _is_proc_line(ln: str) -> bool:
    s = ln.strip()
    if not s:
        return False  # 空行不算过程行，由收集逻辑决定去留
    return bool(_PROC_LINE.match(s))


def extract_reply(output: str, min_len: int = 8) -> str | None:
    """尾部扫描提取模型正文；不足 min_len 返回 None（调用方退回原始尾部）。"""
    if not output:
        return None
    lines = _ANSI2.sub("", _ansi(output)).splitlines()
    keep: list[str] = []
    fence = 0
    for ln in reversed(lines):
        if ln.strip().startswith(_FENCE):
            fence += 1
            keep.append(ln)
            continue
        if fence % 2 == 1:      # 代码块内：一切保留
            keep.append(ln)
            continue
        if _is_proc_line(ln):
            if any(k.strip() for k in keep):
                break           # 已在正文里，遇到过程行即到边界
            continue           # 尾部纯过程行：剥掉
        keep.append(ln)
    # 去掉首尾空行
    while keep and not keep[-1].strip():
        keep.pop()
    body = "\n".join(reversed(keep)).strip("\n")
    if len(body.strip()) < min_len:
        return None
    return body
