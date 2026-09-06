"""工作区快照/差分/恢复（任务可续 handoff 的节点侧，PLAN §13）。

V0.5 策略：文本文件内联（≤MAX_FILES 个、单文件 ≤MAX_FILE_BYTES、总量 ≤MAX_TOTAL_BYTES），
走 WS 控制面；超限或二进制跳过并记录清单。V1 换 Git/对象存储数据面。
"""
from __future__ import annotations

import os
from pathlib import Path

MAX_FILES = 20
MAX_FILE_BYTES = 32 * 1024
MAX_TOTAL_BYTES = 128 * 1024

SKIP_DIRS = {"__pycache__", ".git", "node_modules", ".venv", ".fabric", ".idea", ".vscode"}
SKIP_SUFFIX = {".pyc", ".pyo", ".so", ".db", ".sqlite3", ".log", ".tar", ".gz", ".zip", ".png", ".jpg", ".jpeg", ".pdf"}


def _iter_files(root: Path):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fn in filenames:
            if fn.endswith(tuple(SKIP_SUFFIX)) or fn.startswith("."):
                continue
            p = Path(dirpath) / fn
            if p.is_file() and not p.is_symlink():
                yield p


def snapshot(root: Path) -> dict[str, tuple[int, int]]:
    """工作区指纹：relpath -> (mtime_ns, size)。"""
    out = {}
    for p in _iter_files(root):
        try:
            st = p.stat()
            out[str(p.relative_to(root))] = (st.st_mtime_ns, st.st_size)
        except OSError:
            continue
    return out


def collect_changed(root: Path, before: dict[str, tuple[int, int]]) -> dict:
    """差分出新增/变更文件并读成文本。返回 {files: {relpath: content}, skipped: [relpath]}。"""
    files: dict[str, str] = {}
    skipped: list[str] = []
    total = 0
    for p in _iter_files(root):
        rel = str(p.relative_to(root))
        try:
            st = p.stat()
        except OSError:
            continue
        if before.get(rel) == (st.st_mtime_ns, st.st_size):
            continue  # 未变
        if len(files) + len(skipped) >= MAX_FILES:
            skipped.append(rel + " (超出文件数上限)")
            continue
        if st.st_size > MAX_FILE_BYTES:
            skipped.append(rel + f" (单文件超{MAX_FILE_BYTES // 1024}KB)")
            continue
        try:
            text = p.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            skipped.append(rel + " (二进制/不可读)")
            continue
        if total + len(text) > MAX_TOTAL_BYTES:
            skipped.append(rel + " (总量超限)")
            continue
        files[rel] = text
        total += len(text)
    return {"files": files, "skipped": skipped}


def restore(root: Path, files: dict[str, str]) -> list[str]:
    """把 handoff 文件写回工作区（拒绝路径逃逸）。返回写入的 relpath 列表。"""
    written = []
    for rel, content in (files or {}).items():
        rel = str(rel).replace("\\", "/").lstrip("/")
        if ".." in rel.split("/") or rel.startswith("/"):
            continue
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        written.append(rel)
    return written
