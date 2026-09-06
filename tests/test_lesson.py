"""LESSON 回流机制（V0.9.2）：模型自标经验，无则不回流。"""
from __future__ import annotations

import re

from fabric.central.reply_clean import extract_reply


def _lesson_from(output: str) -> str | None:
    """复刻 daemon._emit_memory_candidate 的解析逻辑做单测。"""
    m = re.findall(r"\[LESSON\]\s*(.+)", output or "")
    lesson = (m[-1] if m else "").strip()
    if not lesson or lesson.lower() in ("无", "none", "n/a", "-"):
        return None
    return lesson


def test_lesson_extracted_from_tail():
    out = "> build · m\n$ python3 x.py\nOK\n完成了。\n[LESSON] matplotlib中文字体要配置 rcParams 否则乱码"
    assert _lesson_from(out) == "matplotlib中文字体要配置 rcParams 否则乱码"


def test_lesson_none_suppressed():
    assert _lesson_from("干完了\n[LESSON] 无") is None
    assert _lesson_from("干完了，没有 LESSON 标记") is None


def test_lesson_last_wins():
    out = "[LESSON] 第一条被覆盖\n[LESSON] 最终采用 venv 内 python"
    assert "venv" in _lesson_from(out)


def test_lesson_stripped_from_user_reply():
    raw = "$ ls\na.txt\n全部完成。\n[LESSON] 麦片部署须同步 site-packages"
    r = extract_reply(raw)
    assert r and "全部完成" in r
    assert "[LESSON]" not in r and "site-packages" not in r
