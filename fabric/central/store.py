"""中央持久化（V0：SQLite + WAL；表结构对齐 docs/PLAN.md §25，PG+pgvector 迁移时保持同构）。"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import time
import uuid

_CJK = re.compile(r"[\u4e00-\u9fff]")
_LATIN = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_\-\.]*")


def _tokenize(text: str) -> set[str]:
    """CJK bigram + 拉丁词（小写）。中文按相邻双字切，英文/数字连串成词。"""
    text = text or ""
    toks = {m.group(0).lower() for m in _LATIN.finditer(text) if len(m.group(0)) >= 2}
    runs = []
    cur = ""
    for ch in text:
        if _CJK.match(ch):
            cur += ch
        else:
            if cur:
                runs.append(cur)
                cur = ""
    if cur:
        runs.append(cur)
    for run in runs:
        for i in range(len(run) - 1):
            toks.add(run[i:i + 2])
    return toks

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks(
  id TEXT PRIMARY KEY, goal TEXT, harness TEXT, node_id TEXT,
  status TEXT, result TEXT, created_at REAL, updated_at REAL);
CREATE TABLE IF NOT EXISTS runs(
  id TEXT PRIMARY KEY, task_id TEXT, harness TEXT, model TEXT,
  status TEXT, result TEXT, started_at REAL, ended_at REAL);
CREATE TABLE IF NOT EXISTS events(
  id TEXT PRIMARY KEY, ts REAL, type TEXT, node_id TEXT, task_id TEXT, payload TEXT);
CREATE TABLE IF NOT EXISTS memory_candidates(
  id TEXT PRIMARY KEY, kind TEXT, content TEXT, source_task TEXT,
  status TEXT DEFAULT 'pending', created_at REAL);
CREATE TABLE IF NOT EXISTS memories(
  id TEXT PRIMARY KEY, kind TEXT, scope TEXT, project TEXT, content TEXT,
  keywords TEXT, importance INTEGER DEFAULT 1, confidence REAL DEFAULT 0.5,
  source_task TEXT, created_at REAL, updated_at REAL);
CREATE INDEX IF NOT EXISTS idx_events_task ON events(task_id);
CREATE INDEX IF NOT EXISTS idx_events_type ON events(type);
CREATE INDEX IF NOT EXISTS idx_memories_kind ON memories(kind);
"""


class Store:
    def __init__(self, path: str):
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        self._migrate_memories_embedding()
        self.db.commit()
        self._lock = threading.Lock()

    # ---- tasks ----
    def save_task(self, task: dict):
        with self._lock:
            self.db.execute(
                "INSERT INTO tasks(id,goal,harness,node_id,status,result,created_at,updated_at,internal) VALUES(?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET goal=excluded.goal,harness=excluded.harness,node_id=excluded.node_id,"
                "status=excluded.status,result=excluded.result,updated_at=excluded.updated_at,internal=excluded.internal",
                (task["id"], task.get("goal"), task.get("harness"), task.get("node_id"),
                 task.get("status"), json.dumps(task.get("result"), ensure_ascii=False),
                 task.get("created_at"), task.get("updated_at"), 1 if task.get("internal") else 0))
            self.db.commit()

    # ---- V1.2 Task/Run 分离：runs 访问层 ----
    def save_run(self, run: dict):
        with self._lock:
            self.db.execute(
                "INSERT OR REPLACE INTO runs(id,task_id,harness,model,status,result,started_at,ended_at) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (run["id"], run["task_id"], run.get("harness"), run.get("model"),
                 run.get("status"), json.dumps(run.get("result"), ensure_ascii=False) if run.get("result") else None,
                 run.get("started_at"), run.get("ended_at")))
            self.db.commit()

    def update_run(self, run_id: str, **fields):
        with self._lock:
            for k, v in fields.items():
                self.db.execute(f"UPDATE runs SET {k}=? WHERE id=?", (v, run_id))
            self.db.commit()

    def get_run(self, run_id: str) -> dict | None:
        row = self.db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if not row:
            return None
        r = dict(row)
        r["result"] = json.loads(r["result"]) if r.get("result") else None
        return r

    def list_runs(self, task_id: str) -> list[dict]:
        rows = self.db.execute(
            "SELECT * FROM runs WHERE task_id=? ORDER BY started_at", (task_id,)).fetchall()
        out = []
        for row in rows:
            r = dict(row)
            r["result"] = json.loads(r["result"]) if r.get("result") else None
            out.append(r)
        return out

    def get_task(self, task_id: str) -> dict | None:
        row = self.db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if not row:
            return None
        t = dict(row)
        t["result"] = json.loads(t["result"]) if t["result"] else None
        return t

    def list_tasks(self, limit: int = 50) -> list[dict]:
        rows = self.db.execute("SELECT * FROM tasks ORDER BY updated_at DESC LIMIT ?", (limit,)).fetchall()
        out = []
        for r in rows:
            t = dict(r)
            t["result"] = json.loads(t["result"]) if t["result"] else None
            out.append(t)
        return out

    # ---- events ----
    def add_event(self, env: dict):
        with self._lock:
            self.db.execute("INSERT OR REPLACE INTO events(id,ts,type,node_id,task_id,payload) VALUES(?,?,?,?,?,?)",
                            (env.get("id") or uuid.uuid4().hex[:12], env.get("ts") or time.time(),
                             env.get("type"), env.get("node_id"), env.get("task_id"),
                             json.dumps(env.get("payload", {}), ensure_ascii=False)))
            self.db.commit()

    def list_events(self, limit: int = 200, task_id: str | None = None) -> list[dict]:
        if task_id:
            rows = self.db.execute("SELECT * FROM events WHERE task_id=? ORDER BY ts DESC LIMIT ?", (task_id, limit)).fetchall()
        else:
            rows = self.db.execute("SELECT * FROM events ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()
        out = []
        for r in rows:
            e = dict(r)
            e["payload"] = json.loads(e["payload"]) if e["payload"] else {}
            out.append(e)
        return out

    # ---- memory candidates（节点提交，待人工/规则审阅；对齐 Engram/Mem0 的 capture→review）----
    def add_memory_candidate(self, payload: dict, source_task: str | None = None) -> str:
        mid = "m-" + uuid.uuid4().hex[:10]
        with self._lock:
            self.db.execute("INSERT INTO memory_candidates(id,kind,content,source_task,status,created_at) VALUES(?,?,?,?,?,?)",
                            (mid, payload.get("kind", "experience"), (payload.get("content") or "")[:4000],
                             payload.get("source_task") or source_task, "pending", time.time()))
            self.db.commit()
        return mid

    def list_memory_candidates(self, status: str | None = None, limit: int = 100) -> list[dict]:
        if status:
            rows = self.db.execute("SELECT * FROM memory_candidates WHERE status=? ORDER BY created_at DESC LIMIT ?", (status, limit)).fetchall()
        else:
            rows = self.db.execute("SELECT * FROM memory_candidates ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def review_memory_candidate(self, cid: str, action: str, kind: str | None = None,
                                scope: str = "user", project: str = "*", importance: int = 1) -> dict | None:
        row = self.db.execute("SELECT * FROM memory_candidates WHERE id=?", (cid,)).fetchone()
        if not row:
            return None
        if action == "discard":
            with self._lock:
                self.db.execute("UPDATE memory_candidates SET status='discarded' WHERE id=?", (cid,))
                self.db.commit()
            return {"id": cid, "status": "discarded"}
        mid = "M-" + uuid.uuid4().hex[:10]
        with self._lock:
            self.db.execute("INSERT INTO memories(id,kind,scope,project,content,keywords,importance,confidence,source_task,created_at,updated_at) "
                            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                            (mid, kind or row["kind"], scope, project, row["content"], "", importance, 0.6,
                             row["source_task"], time.time(), time.time()))
            self.db.execute("UPDATE memory_candidates SET status='promoted' WHERE id=?", (cid,))
            self.db.commit()
        return {"id": mid, "candidate": cid, "status": "promoted", "content": row["content"]}

    # ---- memories（已确认记忆；V0.7 检索=BM25×向量融合，见 memory.py）----

    def _migrate_memories_embedding(self):
        """加 embedding BLOB 列（幂等）。"""
        cols = {r[1] for r in self.db.execute("PRAGMA table_info(memories)")}
        if "embedding" not in cols:
            self.db.execute("ALTER TABLE memories ADD COLUMN embedding BLOB")
        if "hits" not in cols:
            self.db.execute("ALTER TABLE memories ADD COLUMN hits INTEGER DEFAULT 0")
        tcols = {r[1] for r in self.db.execute("PRAGMA table_info(tasks)")}
        if "internal" not in tcols:
            self.db.execute("ALTER TABLE tasks ADD COLUMN internal INTEGER DEFAULT 0")
        self.db.commit()

    def add_memory(self, kind: str, scope: str, content: str, importance: int = 1,
                   project: str = "*", source_task: str | None = None,
                   confidence: float = 0.5, embedding: bytes | None = None) -> str:
        """直接入库（task 自动记忆 / 人工注入走这里；节点回流走 candidate→review）。"""
        mid = "M-" + uuid.uuid4().hex[:10]
        with self._lock:
            self.db.execute("INSERT INTO memories(id,kind,scope,project,content,keywords,importance,confidence,source_task,created_at,updated_at,embedding) "
                            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                            (mid, kind, scope, project, (content or "")[:4000], "", importance,
                             confidence, source_task, time.time(), time.time(), embedding))
            self.db.commit()
        return mid

    def set_memory_embedding(self, mid: str, blob: bytes):
        with self._lock:
            self.db.execute("UPDATE memories SET embedding=? WHERE id=?", (blob, mid))
            self.db.commit()

    def update_memory_content(self, mid: str, content: str) -> bool:
        """更正记忆内容；embedding 清空待重算（旧向量与新内容失配）。"""
        with self._lock:
            cur = self.db.execute("UPDATE memories SET content=?, updated_at=?, embedding=NULL WHERE id=?",
                                  (content[:4000], time.time(), mid))
            self.db.commit()
            return cur.rowcount > 0

    def delete_memory(self, mid: str) -> bool:
        with self._lock:
            cur = self.db.execute("DELETE FROM memories WHERE id=?", (mid,))
            self.db.commit()
            return cur.rowcount > 0

    def bump_memory_hits(self, ids: list[str]):
        """检索命中计数 +1（注入即命中，同 evolve 的 injectionCount 语义）。"""
        if not ids:
            return
        with self._lock:
            self.db.executemany("UPDATE memories SET hits=COALESCE(hits,0)+1, updated_at=? WHERE id=?",
                                [(time.time(), i) for i in ids])
            self.db.commit()

    def delete_memory(self, mid: str):
        with self._lock:
            self.db.execute("DELETE FROM memories WHERE id=?", (mid,))
            self.db.commit()

    def all_memories(self, limit: int = 2000) -> list[dict]:
        self._migrate_memories_embedding()
        rows = self.db.execute("SELECT * FROM memories ORDER BY updated_at DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def get_memories_by_task(self, task_id: str) -> list[dict]:
        rows = self.db.execute("SELECT * FROM memories WHERE source_task=? ORDER BY updated_at DESC",
                               (task_id,)).fetchall()
        return [dict(r) for r in rows]

    def list_memories(self, limit: int = 100) -> list[dict]:
        rows = self.db.execute("SELECT * FROM memories ORDER BY updated_at DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def close(self):
        self.db.close()
