"""中央持久化（V0：SQLite + WAL；表结构对齐 docs/PLAN.md §25，PG+pgvector 迁移时保持同构）。"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import uuid

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks(
  id TEXT PRIMARY KEY, goal TEXT, harness TEXT, node_id TEXT,
  status TEXT, result TEXT, created_at REAL, updated_at REAL);
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
        self.db.commit()
        self._lock = threading.Lock()

    # ---- tasks ----
    def save_task(self, task: dict):
        with self._lock:
            self.db.execute(
                "INSERT INTO tasks(id,goal,harness,node_id,status,result,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET goal=excluded.goal,harness=excluded.harness,node_id=excluded.node_id,"
                "status=excluded.status,result=excluded.result,updated_at=excluded.updated_at",
                (task["id"], task.get("goal"), task.get("harness"), task.get("node_id"),
                 task.get("status"), json.dumps(task.get("result"), ensure_ascii=False),
                 task.get("created_at"), task.get("updated_at")))
            self.db.commit()

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
        return {"id": mid, "candidate": cid, "status": "promoted"}

    # ---- memories（已确认记忆；V0 关键词检索，PG+pgvector 后换向量+BM25 融合）----
    def search_memories(self, query: str, k: int = 5) -> list[dict]:
        terms = [w for w in set((query or "").lower().split()) if len(w) >= 2]
        rows = self.db.execute("SELECT * FROM memories ORDER BY importance DESC, updated_at DESC LIMIT 500").fetchall()
        scored = []
        for r in rows:
            content = (r["content"] or "").lower()
            score = sum(1 for t in terms if t in content) * 2
            if score == 0:
                continue
            scored.append((score + (r["importance"] or 1), dict(r)))
        scored.sort(key=lambda x: -x[0])
        out = []
        for _, m in scored[:k]:
            m.pop("keywords", None)
            out.append(m)
        return out

    def list_memories(self, limit: int = 100) -> list[dict]:
        rows = self.db.execute("SELECT * FROM memories ORDER BY updated_at DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def close(self):
        self.db.close()
